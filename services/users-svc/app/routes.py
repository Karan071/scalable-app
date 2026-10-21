"""users-svc HTTP routes (LLD-01 §11.3).

Platform-admin routes (``/v1/platform/users*``) land in E2.10: Casbin
``platform_user:{read,manage}`` (via ``Authorize(..., domain="platform")``)
plus BFF step-up (acr>=2, floored automatically for any platform-domain
route by ``Authorize``'s own ``min_acr`` logic) were the two prerequisites
named in ``service.py``'s old docstring, and both now exist (E2.7, E2.4).
"""

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sf_authz import AuthenticatedUser, Authorize, Internal, Principal
from sf_db import transaction
from sqlalchemy.ext.asyncio import AsyncSession

from . import repository, service
from .db import get_session
from .models import User

router = APIRouter()
logger = logging.getLogger(__name__)

# Named so tests can target them with `app.dependency_overrides[...]`
# (FastAPI keys overrides by the exact callable passed to `Depends`).
require_provision_scope = Internal("users:provision")
require_read_scope = Internal("users:read")
require_authenticated_user = AuthenticatedUser()
require_read_platform_user = Authorize("platform_user", "read", domain="platform")
require_manage_platform_user = Authorize("platform_user", "manage", domain="platform")


def _sf_uid(principal: Principal) -> UUID:
    raw = principal.claim("sf_uid")
    if raw is None:
        raise HTTPException(
            status_code=403, detail="token has no sf_uid claim yet; refresh required"
        )
    return UUID(str(raw))


class ProvisionRequest(BaseModel):
    idp_issuer: str
    idp_subject: str
    email: str
    email_verified: bool


class ProvisionResponse(BaseModel):
    user_id: UUID
    status: str
    needs_claim_refresh: bool


@router.post("/internal/v1/users:provision", response_model=ProvisionResponse)
async def provision(
    body: ProvisionRequest,
    request: Request,
    _: Principal = Depends(require_provision_scope),
) -> ProvisionResponse:
    # Not `Depends(get_session)`: the Keycloak admin sync below is a network
    # call that must run *after* this transaction commits (LLD-09 §3), but
    # also *before* the response is returned — the BFF refreshes the token
    # for `sf_uid` as soon as it sees `needs_claim_refresh`, so a
    # fire-and-forget background task would race it.
    async with transaction(request.app.state.session_factory) as session:
        result = await service.provision_user(
            session,
            idp_issuer=body.idp_issuer,
            idp_subject=body.idp_subject,
            email=body.email,
            email_verified=body.email_verified,
        )

    if result.needs_claim_refresh:
        try:
            await request.app.state.keycloak_admin.set_sf_uid(
                keycloak_user_id=body.idp_subject, sf_uid=str(result.user_id)
            )
        except Exception:
            # LLD-01 §11.2 plans an async retry via the outbox command
            # `user.idp_sync_requested` for this failure path; that command
            # handler doesn't exist yet (open item), so this just logs.
            logger.exception(
                "failed to sync sf_uid to Keycloak", extra={"sf_uid": str(result.user_id)}
            )

    return ProvisionResponse(
        user_id=result.user_id, status=result.status, needs_claim_refresh=result.needs_claim_refresh
    )


class MeResponse(BaseModel):
    id: UUID
    handle: str | None
    display_name: str | None
    email: str
    status: str
    version: int


@router.get("/v1/me", response_model=MeResponse)
async def get_me(
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_authenticated_user),
) -> MeResponse:
    user = await service.get_self(session, _sf_uid(principal))
    if user is None:
        raise HTTPException(status_code=404)
    return MeResponse(
        id=user.id,
        handle=user.handle,
        display_name=user.display_name,
        email=user.email,
        status=user.status,
        version=user.version,
    )


class UpdateMeRequest(BaseModel):
    display_name: str | None = None
    handle: str | None = None


@router.patch("/v1/me", response_model=MeResponse)
async def patch_me(
    body: UpdateMeRequest,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_authenticated_user),
) -> MeResponse:
    try:
        user = await service.update_self(
            session, _sf_uid(principal), display_name=body.display_name, handle=body.handle
        )
    except service.HandleUnavailable as exc:
        raise HTTPException(status_code=409, detail="handle unavailable") from exc
    except service.UserNotFoundOrAlreadyInState as exc:
        raise HTTPException(status_code=404) from exc
    except repository.VersionConflict as exc:
        raise HTTPException(status_code=409, detail="concurrent update, retry") from exc
    return MeResponse(
        id=user.id,
        handle=user.handle,
        display_name=user.display_name,
        email=user.email,
        status=user.status,
        version=user.version,
    )


class HandleAvailability(BaseModel):
    available: bool


@router.get("/v1/me/handle-availability", response_model=HandleAvailability)
async def handle_availability(
    handle: str = Query(..., min_length=1, max_length=30),
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_authenticated_user),
) -> HandleAvailability:
    available = await service.check_handle_availability(session, handle)
    return HandleAvailability(available=available)


class BatchUser(BaseModel):
    id: UUID
    status: str
    display_name: str | None
    handle: str | None


@router.get("/internal/v1/users:batch", response_model=list[BatchUser])
async def batch(
    ids: list[UUID] = Query(...),
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_read_scope),
) -> list[BatchUser]:
    users = await service.get_batch(session, ids)
    return [
        BatchUser(id=u.id, status=u.status, display_name=u.display_name, handle=u.handle)
        for u in users
    ]


class PlatformUserResponse(BaseModel):
    id: UUID
    handle: str | None
    display_name: str | None
    email: str
    status: str
    version: int


def _platform_user_out(u: User) -> PlatformUserResponse:
    return PlatformUserResponse(
        id=u.id,
        handle=u.handle,
        display_name=u.display_name,
        email=u.email,
        status=u.status,
        version=u.version,
    )


@router.get("/v1/platform/users", response_model=list[PlatformUserResponse])
async def list_platform_users(
    search: str | None = Query(default=None, max_length=100),
    cursor: UUID | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_read_platform_user),
) -> list[PlatformUserResponse]:
    users = await repository.list_users(session, search=search, cursor_id=cursor, limit=limit)
    return [_platform_user_out(u) for u in users]


class PlatformDisableUserRequest(BaseModel):
    reason: str = Field(min_length=10, max_length=200)


@router.post("/v1/platform/users/{user_id}:disable", response_model=None)
async def disable_platform_user(
    user_id: UUID,
    body: PlatformDisableUserRequest,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_manage_platform_user),
) -> dict[str, str]:
    try:
        await service.disable_user(session, user_id, reason=body.reason)
    except service.UserNotFoundOrAlreadyInState as exc:
        raise HTTPException(status_code=404) from exc
    return {"status": "disabled"}


@router.post("/v1/platform/users/{user_id}:reactivate", response_model=None)
async def reactivate_platform_user(
    user_id: UUID,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_manage_platform_user),
) -> dict[str, str]:
    try:
        await service.reactivate_user(session, user_id)
    except service.UserNotFoundOrAlreadyInState as exc:
        raise HTTPException(status_code=404) from exc
    return {"status": "active"}
