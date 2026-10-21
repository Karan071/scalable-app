"""users-svc business logic: provisioning and lifecycle (LLD-01 §11.2, §11.3).

Platform-admin disable/reactivate are exposed via
``POST /v1/platform/users/{id}:disable|:reactivate`` (routes.py, E2.10),
gated on Casbin ``platform_user:manage`` plus the BFF's step-up flow.
"""

from dataclasses import dataclass
from uuid import UUID

from sf_messaging import add_to_outbox, build_envelope
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from . import repository
from .handles import normalize_handle
from .models import User

_NOTIFY_CHANNEL = "outbox_users"


class UserNotFoundOrAlreadyInState(Exception):
    pass


class HandleUnavailable(Exception):
    pass


@dataclass(frozen=True)
class ProvisionResult:
    user_id: UUID
    status: str
    needs_claim_refresh: bool


async def _emit(
    session: AsyncSession, user: User, *, event_type: str, routing_key: str, **data: object
) -> None:
    envelope = build_envelope(
        event_type=event_type,
        source="users-svc",
        subject=str(user.id),
        data={"user_id": str(user.id), "status": user.status, **data},
        schema_version="1.0",
        aggregate_type="user",
        aggregate_version=user.version,
    )
    await add_to_outbox(
        session,
        envelope,
        exchange="sf.events",
        routing_key=routing_key,
        aggregate_id=str(user.id),
        notify_channel=_NOTIFY_CHANNEL,
    )


async def provision_user(
    session: AsyncSession,
    *,
    idp_issuer: str,
    idp_subject: str,
    email: str,
    email_verified: bool,
) -> ProvisionResult:
    """``POST /internal/v1/users:provision`` (LLD-01 §4, §11.3). Idempotent."""
    display_name = email.split("@", 1)[0]
    created = await repository.insert_user_if_absent(
        session,
        idp_issuer=idp_issuer,
        idp_subject=idp_subject,
        email=email,
        email_verified=email_verified,
        display_name=display_name,
    )
    if created is not None:
        await _emit(session, created, event_type="user.created", routing_key="user.created.v1")
        return ProvisionResult(user_id=created.id, status=created.status, needs_claim_refresh=True)

    existing = await repository.get_by_idp_identity(
        session, idp_issuer=idp_issuer, idp_subject=idp_subject
    )
    if existing is None:
        raise AssertionError("unique constraint guarantees a row exists after a conflict")
    return ProvisionResult(user_id=existing.id, status=existing.status, needs_claim_refresh=False)


async def get_self(session: AsyncSession, user_id: UUID) -> User | None:
    return await repository.get_by_id(session, user_id)


async def get_batch(session: AsyncSession, ids: list[UUID]) -> list[User]:
    return await repository.list_by_ids(session, ids)


async def check_handle_availability(session: AsyncSession, handle: str) -> bool:
    try:
        normalized = normalize_handle(handle)
    except ValueError:
        return False
    return not await repository.is_handle_taken(session, normalized)


async def update_self(
    session: AsyncSession,
    user_id: UUID,
    *,
    display_name: str | None,
    handle: str | None,
) -> User:
    """``PATCH /v1/me``. Reads the current version and writes within the same
    transaction; it has no client-supplied ``If-Match`` contract (open item
    if concurrent self-edits ever need it — profile edits are low-contention,
    unlike the invariants LLD-09 §3 lists optimistic locking for).
    """
    current = await repository.get_by_id(session, user_id)
    if current is None:
        raise UserNotFoundOrAlreadyInState(user_id)

    handle_normalized = normalize_handle(handle) if handle is not None else None
    if handle_normalized is not None and await repository.is_handle_taken(
        session, handle_normalized
    ):
        raise HandleUnavailable(handle)

    try:
        return await repository.update_profile(
            session,
            user_id,
            expected_version=current.version,
            display_name=display_name,
            handle=handle,
            handle_normalized=handle_normalized,
        )
    except IntegrityError as exc:
        if handle is not None:
            raise HandleUnavailable(handle) from exc
        raise


async def disable_user(session: AsyncSession, user_id: UUID, *, reason: str) -> None:
    user = await repository.disable_user(session, user_id, reason=reason)
    if user is None:
        raise UserNotFoundOrAlreadyInState(user_id)
    await _emit(
        session, user, event_type="user.disabled", routing_key="user.disabled.v1", reason=reason
    )


async def reactivate_user(session: AsyncSession, user_id: UUID) -> None:
    user = await repository.reactivate_user(session, user_id)
    if user is None:
        raise UserNotFoundOrAlreadyInState(user_id)
    await _emit(session, user, event_type="user.reactivated", routing_key="user.reactivated.v1")
