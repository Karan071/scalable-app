"""orgs-svc HTTP routes (LLD-03 §8).

None of these declare `Authorize(...)` yet (LLD-02 §6) — that needs the
Casbin local enforcer (E2.7, open item). In its place, org-scoped routes
require the caller to be an *active member* of the path org (checked
locally against this service's own `memberships` table) — real tenant
isolation (FR-ORG-04), just not role-grained. Platform routes only require
an authenticated caller for now (open item: `platform_org:*` + step-up).
"""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sf_authz import AuthenticatedUser, Internal, Principal
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from . import repository, service
from .db import get_session
from .models import Invitation, Membership, Organization

router = APIRouter()

require_authenticated_user = AuthenticatedUser()
require_internal_read_scope = Internal("orgs:read")


def _sf_uid(principal: Principal) -> UUID:
    raw = principal.claim("sf_uid")
    if raw is None:
        raise HTTPException(
            status_code=403, detail="token has no sf_uid claim yet; refresh required"
        )
    return UUID(str(raw))


async def _require_active_membership(
    session: AsyncSession, *, org_id: UUID, user_id: UUID
) -> Membership:
    membership = await repository.get_membership_for_user(session, org_id=org_id, user_id=user_id)
    if membership is None or membership.status != "active":
        # 404, not 403: don't confirm the org exists to a non-member (LLD-02 §5).
        raise HTTPException(status_code=404)
    return membership


def _org_out(org: Organization) -> "OrganizationResponse":
    return OrganizationResponse(
        id=org.id,
        slug=org.slug,
        name=org.name,
        status=org.status,
        currency=org.currency,
        shipping_flat_minor=org.shipping_flat_minor,
        tax_rate_bps=org.tax_rate_bps,
        settings=org.settings,
        version=org.version,
    )


def _membership_out(m: Membership) -> "MembershipResponse":
    return MembershipResponse(
        id=m.id, org_id=m.org_id, user_id=m.user_id, status=m.status, version=m.version
    )


def _invitation_out(i: Invitation) -> "InvitationResponse":
    return InvitationResponse(
        id=i.id,
        org_id=i.org_id,
        email=i.email,
        roles_requested=i.roles_requested,
        status=i.status,
        expires_at=i.expires_at,
    )


class OrganizationResponse(BaseModel):
    id: UUID
    slug: str
    name: str
    status: str
    currency: str
    shipping_flat_minor: int
    tax_rate_bps: int
    settings: dict[str, object]
    version: int


class CreateOrganizationRequest(BaseModel):
    slug: str
    name: str
    owner_email: str
    currency: str = Field(min_length=3, max_length=3)
    shipping_flat_minor: int = 0
    tax_rate_bps: int = 0


@router.post("/v1/platform/orgs", response_model=OrganizationResponse)
async def create_organization(
    body: CreateOrganizationRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_authenticated_user),
) -> OrganizationResponse:
    try:
        created = await service.create_organization(
            session,
            slug=body.slug,
            name=body.name,
            currency=body.currency.upper(),
            shipping_flat_minor=body.shipping_flat_minor,
            tax_rate_bps=body.tax_rate_bps,
            created_by=_sf_uid(principal),
            owner_email=body.owner_email,
            ttl_days=request.app.state.settings.invitation_ttl_days,
            cipher=request.app.state.invitation_cipher,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _org_out(created.organization)


@router.get("/v1/platform/orgs", response_model=list[OrganizationResponse])
async def list_platform_organizations(
    status: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_authenticated_user),
) -> list[OrganizationResponse]:
    orgs = await repository.list_organizations(session, status=status)
    return [_org_out(o) for o in orgs]


class TransitionRequest(BaseModel):
    reason: str | None = None


@router.post("/v1/platform/orgs/{org_id}:suspend", response_model=OrganizationResponse)
async def suspend_organization(
    org_id: UUID,
    body: TransitionRequest,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_authenticated_user),
) -> OrganizationResponse:
    return await _transition_org(session, org_id, action="suspend", reason=body.reason)


@router.post("/v1/platform/orgs/{org_id}:reactivate", response_model=OrganizationResponse)
async def reactivate_organization(
    org_id: UUID,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_authenticated_user),
) -> OrganizationResponse:
    return await _transition_org(session, org_id, action="reactivate", reason=None)


@router.post("/v1/platform/orgs/{org_id}:archive", response_model=OrganizationResponse)
async def archive_organization(
    org_id: UUID,
    body: TransitionRequest,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_authenticated_user),
) -> OrganizationResponse:
    return await _transition_org(session, org_id, action="archive", reason=body.reason)


async def _transition_org(
    session: AsyncSession, org_id: UUID, *, action: str, reason: str | None
) -> OrganizationResponse:
    try:
        org = await service.transition_organization(session, org_id, action=action, reason=reason)
    except service.NotFoundError as exc:
        raise HTTPException(status_code=404) from exc
    except service.InvalidTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _org_out(org)


@router.get("/v1/orgs/{org_id}", response_model=OrganizationResponse)
async def get_organization(
    org_id: UUID,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_authenticated_user),
) -> OrganizationResponse:
    await _require_active_membership(session, org_id=org_id, user_id=_sf_uid(principal))
    org = await repository.get_organization(session, org_id)
    if org is None:
        raise HTTPException(status_code=404)
    return _org_out(org)


class UpdateOrganizationRequest(BaseModel):
    name: str | None = None
    shipping_flat_minor: int | None = None
    tax_rate_bps: int | None = None
    settings: dict[str, object] | None = None


@router.patch("/v1/orgs/{org_id}", response_model=OrganizationResponse)
async def update_organization(
    org_id: UUID,
    body: UpdateOrganizationRequest,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_authenticated_user),
) -> OrganizationResponse:
    await _require_active_membership(session, org_id=org_id, user_id=_sf_uid(principal))
    current = await repository.get_organization(session, org_id)
    if current is None:
        raise HTTPException(status_code=404)
    try:
        org = await service.update_organization_settings(
            session,
            org_id,
            expected_version=current.version,
            name=body.name,
            shipping_flat_minor=body.shipping_flat_minor,
            tax_rate_bps=body.tax_rate_bps,
            settings=body.settings,
        )
    except service.VersionConflictError as exc:
        raise HTTPException(status_code=409, detail="concurrent update, retry") from exc
    return _org_out(org)


class MembershipResponse(BaseModel):
    id: UUID
    org_id: UUID
    user_id: UUID
    status: str
    version: int


@router.get("/v1/orgs/{org_id}/members", response_model=list[MembershipResponse])
async def list_members(
    org_id: UUID,
    status: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_authenticated_user),
) -> list[MembershipResponse]:
    await _require_active_membership(session, org_id=org_id, user_id=_sf_uid(principal))
    members = await repository.list_memberships(session, org_id, status=status)
    return [_membership_out(m) for m in members]


@router.post("/v1/orgs/{org_id}/members/{membership_id}:disable", response_model=None)
async def disable_member(
    org_id: UUID,
    membership_id: UUID,
    body: TransitionRequest,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_authenticated_user),
) -> dict[str, str]:
    actor_id = _sf_uid(principal)
    await _require_active_membership(session, org_id=org_id, user_id=actor_id)
    try:
        await service.disable_membership(
            session, membership_id, org_id=org_id, actor_id=actor_id, reason=body.reason
        )
    except service.NotFoundError as exc:
        raise HTTPException(status_code=404) from exc
    return {"status": "disabled"}


@router.post("/v1/orgs/{org_id}/members/{membership_id}:reactivate", response_model=None)
async def reactivate_member(
    org_id: UUID,
    membership_id: UUID,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_authenticated_user),
) -> dict[str, str]:
    actor_id = _sf_uid(principal)
    await _require_active_membership(session, org_id=org_id, user_id=actor_id)
    try:
        await service.reactivate_membership(
            session, membership_id, org_id=org_id, actor_id=actor_id
        )
    except service.NotFoundError as exc:
        raise HTTPException(status_code=404) from exc
    return {"status": "active"}


@router.delete("/v1/orgs/{org_id}/members/{membership_id}", response_model=None)
async def revoke_member(
    org_id: UUID,
    membership_id: UUID,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_authenticated_user),
) -> dict[str, str]:
    actor_id = _sf_uid(principal)
    await _require_active_membership(session, org_id=org_id, user_id=actor_id)
    try:
        await service.revoke_membership(
            session, membership_id, org_id=org_id, actor_id=actor_id, reason="removed"
        )
    except service.NotFoundError as exc:
        raise HTTPException(status_code=404) from exc
    return {"status": "revoked"}


@router.post("/v1/orgs/{org_id}/members/me:leave", response_model=None)
async def leave_organization(
    org_id: UUID,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_authenticated_user),
) -> dict[str, str]:
    actor_id = _sf_uid(principal)
    membership = await _require_active_membership(session, org_id=org_id, user_id=actor_id)
    await service.revoke_membership(
        session, membership.id, org_id=org_id, actor_id=actor_id, reason="self_leave"
    )
    return {"status": "revoked"}


class InvitationResponse(BaseModel):
    id: UUID
    org_id: UUID
    email: str
    roles_requested: list[str]
    status: str
    expires_at: datetime


@router.get("/v1/orgs/{org_id}/invitations", response_model=list[InvitationResponse])
async def list_invitations(
    org_id: UUID,
    status: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_authenticated_user),
) -> list[InvitationResponse]:
    await _require_active_membership(session, org_id=org_id, user_id=_sf_uid(principal))
    invitations = await repository.list_invitations(session, org_id, status=status)
    return [_invitation_out(i) for i in invitations]


class CreateInvitationRequest(BaseModel):
    email: str
    roles_requested: list[str] = Field(min_length=1)


class CreateInvitationResponse(BaseModel):
    invitation_id: UUID
    expires_at: datetime


@router.post("/v1/orgs/{org_id}/invitations", response_model=CreateInvitationResponse)
async def create_invitation(
    org_id: UUID,
    body: CreateInvitationRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_authenticated_user),
) -> CreateInvitationResponse:
    actor_id = _sf_uid(principal)
    await _require_active_membership(session, org_id=org_id, user_id=actor_id)
    try:
        result = await service.create_invitation(
            session,
            org_id=org_id,
            email=body.email,
            roles_requested=body.roles_requested,
            invited_by=actor_id,
            ttl_days=request.app.state.settings.invitation_ttl_days,
            cipher=request.app.state.invitation_cipher,
        )
    except IntegrityError as exc:
        # Partial UNIQUE(org_id, email) WHERE status='pending' (LLD-03 §3).
        raise HTTPException(
            status_code=409, detail="a pending invitation already exists for this email"
        ) from exc
    return CreateInvitationResponse(
        invitation_id=result.invitation_id, expires_at=result.expires_at
    )


@router.post(
    "/v1/orgs/{org_id}/invitations/{invitation_id}:resend", response_model=CreateInvitationResponse
)
async def resend_invitation(
    org_id: UUID,
    invitation_id: UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_authenticated_user),
) -> CreateInvitationResponse:
    await _require_active_membership(session, org_id=org_id, user_id=_sf_uid(principal))
    try:
        result = await service.resend_invitation(
            session,
            invitation_id,
            org_id=org_id,
            ttl_days=request.app.state.settings.invitation_ttl_days,
            cipher=request.app.state.invitation_cipher,
        )
    except service.NotFoundError as exc:
        raise HTTPException(status_code=404) from exc
    return CreateInvitationResponse(
        invitation_id=result.invitation_id, expires_at=result.expires_at
    )


@router.delete("/v1/orgs/{org_id}/invitations/{invitation_id}", response_model=None)
async def revoke_invitation(
    org_id: UUID,
    invitation_id: UUID,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_authenticated_user),
) -> dict[str, str]:
    await _require_active_membership(session, org_id=org_id, user_id=_sf_uid(principal))
    try:
        await service.revoke_invitation(session, invitation_id, org_id=org_id)
    except service.NotFoundError as exc:
        raise HTTPException(status_code=404) from exc
    return {"status": "revoked"}


class AcceptInvitationRequest(BaseModel):
    token: str


class AcceptInvitationResponse(BaseModel):
    org_id: UUID


@router.post("/v1/invitations:accept", response_model=AcceptInvitationResponse)
async def accept_invitation(
    body: AcceptInvitationRequest,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_authenticated_user),
) -> AcceptInvitationResponse:
    try:
        result = await service.accept_invitation(
            session,
            token=body.token,
            user_id=_sf_uid(principal),
            user_email=str(principal.claim("email") or ""),
            user_email_verified=bool(principal.claim("email_verified")),
        )
    except service.InvitationInvalid as exc:
        raise HTTPException(status_code=410, detail="invitation-invalid") from exc
    except service.InvitationEmailMismatch as exc:
        raise HTTPException(status_code=403, detail="invitation-email-mismatch") from exc
    return AcceptInvitationResponse(org_id=result.org_id)


@router.get("/v1/me/memberships", response_model=list[MembershipResponse])
async def my_memberships(
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_authenticated_user),
) -> list[MembershipResponse]:
    memberships = await repository.list_memberships_for_user(session, _sf_uid(principal))
    return [_membership_out(m) for m in memberships]


@router.get("/internal/v1/memberships/{membership_id}", response_model=MembershipResponse)
async def internal_get_membership(
    membership_id: UUID,
    org_id: UUID = Query(...),
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_internal_read_scope),
) -> MembershipResponse:
    membership = await repository.get_membership(session, membership_id, org_id=org_id)
    if membership is None:
        raise HTTPException(status_code=404)
    return _membership_out(membership)
