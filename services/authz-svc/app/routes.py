"""authz-svc HTTP routes (LLD-02 §12). authz-svc dogfoods its own enforcer:
every route below declares ``Authorize(...)`` except the two self-scoped reads.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sf_authz import AuthenticatedUser, Authorize, PolicyEnforcer, Principal, org_domain_from_path
from sf_db import transaction
from sqlalchemy.ext.asyncio import AsyncSession

from . import repository, service
from .db import get_session
from .models import Role, RoleAssignment
from .orgs_client import OrgsClient

router = APIRouter()

require_authenticated_user = AuthenticatedUser()


_org_domain = org_domain_from_path()

require_read_role_assignment = Authorize("role_assignment", "read", domain=_org_domain)
require_assign_role_assignment = Authorize("role_assignment", "assign", domain=_org_domain)
require_revoke_role_assignment = Authorize("role_assignment", "revoke", domain=_org_domain)

require_read_platform_role_assignment = Authorize(
    "role_assignment", "read", domain="platform", min_acr=2
)
require_assign_platform_role_assignment = Authorize(
    "role_assignment",
    "assign",
    domain="platform",
    min_acr=2,
    max_auth_age_s=service.PLATFORM_GRANT_MAX_AUTH_AGE_S,
)
require_revoke_platform_role_assignment = Authorize(
    "role_assignment",
    "revoke",
    domain="platform",
    min_acr=2,
    max_auth_age_s=service.PLATFORM_GRANT_MAX_AUTH_AGE_S,
)


def _sf_uid(principal: Principal) -> UUID:
    raw = principal.claim("sf_uid")
    if raw is None:
        raise HTTPException(
            status_code=403, detail="token has no sf_uid claim yet; refresh required"
        )
    return UUID(str(raw))


def _enforcer(request: Request) -> PolicyEnforcer:
    return request.app.state.authz_enforcer  # type: ignore[no-any-return]


def _orgs_client(request: Request) -> OrgsClient:
    return request.app.state.orgs_client  # type: ignore[no-any-return]


async def _publish_policy_version(request: Request, version: int) -> None:
    try:
        await request.app.state.redis.publish("authz:policy", str(version))
    except Exception:  # best-effort; the 10s poll fallback still converges
        pass


class RoleAssignmentResponse(BaseModel):
    id: UUID
    user_id: UUID
    role_id: str
    domain: str
    org_id: UUID | None
    status: str
    granted_by: UUID
    version: int


def _assignment_out(a: RoleAssignment) -> RoleAssignmentResponse:
    return RoleAssignmentResponse(
        id=a.id,
        user_id=a.user_id,
        role_id=a.role_id,
        domain=a.domain,
        org_id=a.org_id,
        status=a.status,
        granted_by=a.granted_by,
        version=a.version,
    )


class RoleResponse(BaseModel):
    id: str
    scope: str
    description: str
    requires_mfa: bool


def _role_out(r: Role) -> RoleResponse:
    return RoleResponse(
        id=r.id, scope=r.scope, description=r.description, requires_mfa=r.requires_mfa
    )


class AssignRoleRequest(BaseModel):
    user_id: UUID
    role_id: str
    membership_id: UUID
    reason: str | None = None


@router.get("/v1/orgs/{org_id}/role-assignments", response_model=list[RoleAssignmentResponse])
async def list_org_role_assignments(
    org_id: UUID,
    user_id: UUID | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_read_role_assignment),
) -> list[RoleAssignmentResponse]:
    assignments = await repository.list_role_assignments(
        session, domain=f"org:{org_id}", user_id=user_id
    )
    return [_assignment_out(a) for a in assignments]


@router.post("/v1/orgs/{org_id}/role-assignments", response_model=RoleAssignmentResponse)
async def assign_org_role(
    org_id: UUID,
    body: AssignRoleRequest,
    request: Request,
    principal: Principal = Depends(require_assign_role_assignment),
) -> RoleAssignmentResponse:
    actor_id = _sf_uid(principal)
    # Explicit transaction, not Depends(get_session): the publish below must
    # follow the commit (LLD-02 §7 step 8), and a dependency commits only
    # after the handler returns.
    async with transaction(request.app.state.session_factory) as session:
        try:
            result = await service.assign_role(
                session,
                org_id=org_id,
                membership_id=body.membership_id,
                target_user_id=body.user_id,
                role_id=body.role_id,
                actor_id=actor_id,
                actor_subject=principal.casbin_subject(),
                reason=body.reason,
                enforcer=_enforcer(request),
                orgs_client=_orgs_client(request),
            )
        except service.MembershipNotActive as exc:
            raise HTTPException(status_code=409, detail="membership is not active") from exc
        except service.NotFoundError as exc:
            raise HTTPException(status_code=404, detail="role not found") from exc
        except service.ScopeMismatch as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except service.AssignableByDenied as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except service.SelfGrantForbidden as exc:
            raise HTTPException(status_code=403, detail="cannot grant a role to yourself") from exc
        except repository.AlreadyAssigned as exc:
            raise HTTPException(
                status_code=409, detail="the user already holds this role in this domain"
            ) from exc
    await _publish_policy_version(request, result.policy_version)
    return _assignment_out(result.assignment)


@router.delete("/v1/orgs/{org_id}/role-assignments/{assignment_id}", response_model=None)
async def revoke_org_role(
    org_id: UUID,
    assignment_id: UUID,
    request: Request,
    if_match: int = Header(..., alias="If-Match"),
    reason: str | None = Query(default=None),
    principal: Principal = Depends(require_revoke_role_assignment),
) -> dict[str, str]:
    actor_id = _sf_uid(principal)
    # Explicit transaction, not Depends(get_session): the publish below must
    # follow the commit (LLD-02 §7 step 8), and a dependency commits only
    # after the handler returns.
    async with transaction(request.app.state.session_factory) as session:
        try:
            result = await service.revoke_role(
                session,
                assignment_id,
                domain=f"org:{org_id}",
                expected_version=if_match,
                actor_id=actor_id,
                actor_subject=principal.casbin_subject(),
                reason=reason,
                enforcer=_enforcer(request),
            )
        except service.NotFoundError as exc:
            raise HTTPException(status_code=404) from exc
        except repository.VersionConflict as exc:
            raise HTTPException(
                status_code=412, detail="version conflict, refetch and retry"
            ) from exc
        except service.AssignableByDenied as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except service.LastAdminProtected as exc:
            raise HTTPException(
                status_code=409, detail="cannot revoke the last active org_admin"
            ) from exc
    await _publish_policy_version(request, result.policy_version)
    return {"status": "revoked"}


@router.get("/v1/roles", response_model=list[RoleResponse])
async def list_roles(
    scope: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_authenticated_user),
) -> list[RoleResponse]:
    roles = await repository.list_roles(session, scope=scope)
    return [_role_out(r) for r in roles]


class AssignPlatformRoleRequest(BaseModel):
    user_id: UUID
    role_id: str
    reason: str = Field(min_length=10, max_length=200)


@router.get("/v1/platform/role-assignments", response_model=list[RoleAssignmentResponse])
async def list_platform_role_assignments(
    user_id: UUID | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_read_platform_role_assignment),
) -> list[RoleAssignmentResponse]:
    assignments = await repository.list_role_assignments(
        session, domain="platform", user_id=user_id
    )
    return [_assignment_out(a) for a in assignments]


@router.post("/v1/platform/role-assignments", response_model=RoleAssignmentResponse)
async def assign_platform_role(
    body: AssignPlatformRoleRequest,
    request: Request,
    principal: Principal = Depends(require_assign_platform_role_assignment),
) -> RoleAssignmentResponse:
    actor_id = _sf_uid(principal)
    auth_time = principal.claim("auth_time")
    # Explicit transaction, not Depends(get_session): the publish below must
    # follow the commit (LLD-02 §7 step 8), and a dependency commits only
    # after the handler returns.
    async with transaction(request.app.state.session_factory) as session:
        try:
            result = await service.assign_platform_role(
                session,
                target_user_id=body.user_id,
                role_id=body.role_id,
                actor_id=actor_id,
                actor_subject=principal.casbin_subject(),
                actor_auth_time=float(auth_time) if auth_time is not None else None,
                reason=body.reason,
                enforcer=_enforcer(request),
            )
        except service.NotFoundError as exc:
            raise HTTPException(status_code=404, detail="role not found") from exc
        except service.ScopeMismatch as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except service.AssignableByDenied as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except service.SelfGrantForbidden as exc:
            raise HTTPException(status_code=403, detail="cannot grant a role to yourself") from exc
        except repository.AlreadyAssigned as exc:
            raise HTTPException(
                status_code=409, detail="the user already holds this role in this domain"
            ) from exc
        except (ValueError, PermissionError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _publish_policy_version(request, result.policy_version)
    return _assignment_out(result.assignment)


@router.delete("/v1/platform/role-assignments/{assignment_id}", response_model=None)
async def revoke_platform_role(
    assignment_id: UUID,
    request: Request,
    if_match: int = Header(..., alias="If-Match"),
    reason: str | None = Query(default=None),
    principal: Principal = Depends(require_revoke_platform_role_assignment),
) -> dict[str, str]:
    actor_id = _sf_uid(principal)
    # Explicit transaction, not Depends(get_session): the publish below must
    # follow the commit (LLD-02 §7 step 8), and a dependency commits only
    # after the handler returns.
    async with transaction(request.app.state.session_factory) as session:
        try:
            result = await service.revoke_role(
                session,
                assignment_id,
                domain="platform",
                expected_version=if_match,
                actor_id=actor_id,
                actor_subject=principal.casbin_subject(),
                reason=reason,
                enforcer=_enforcer(request),
            )
        except service.NotFoundError as exc:
            raise HTTPException(status_code=404) from exc
        except repository.VersionConflict as exc:
            raise HTTPException(
                status_code=412, detail="version conflict, refetch and retry"
            ) from exc
        except service.AssignableByDenied as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
    await _publish_policy_version(request, result.policy_version)
    return {"status": "revoked"}


class PermissionsResponse(BaseModel):
    domain: str
    permissions: list[str]


@router.get("/v1/me/permissions", response_model=PermissionsResponse)
async def my_permissions(
    request: Request,
    domain: str = Query(...),
    principal: Principal = Depends(require_authenticated_user),
) -> PermissionsResponse:
    """UI-hint only (§12) — the UI is never trusted with it; the backend
    re-checks every mutation through ``Authorize(...)``."""
    enforcer = _enforcer(request)
    subject = principal.casbin_subject()
    permissions = enforcer.permissions_for(subject, domain)
    return PermissionsResponse(domain=domain, permissions=permissions)


@router.get("/v1/me/role-assignments", response_model=list[RoleAssignmentResponse])
async def my_role_assignments(
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_authenticated_user),
) -> list[RoleAssignmentResponse]:
    """Self-scoped, every domain at once (E2.10): backs the BFF's session
    bootstrap (``memberships[].roles[]``/``platform_roles[]``). No
    ``Authorize(...)`` beyond authentication — reading your own grants
    isn't a privilege question."""
    assignments = await repository.list_active_role_assignments_for_user(
        session, _sf_uid(principal)
    )
    return [_assignment_out(a) for a in assignments]
