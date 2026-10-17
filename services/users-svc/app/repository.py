"""Direct DB access for the ``users`` schema (LLD-01 §11.1)."""

from datetime import UTC, datetime
from uuid import UUID, uuid7

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from .models import ReservedHandle, User


class VersionConflict(Exception):
    pass


async def insert_user_if_absent(
    session: AsyncSession,
    *,
    idp_issuer: str,
    idp_subject: str,
    email: str,
    email_verified: bool,
    display_name: str | None,
) -> User | None:
    """Idempotent insert keyed on ``(idp_issuer, idp_subject)``.

    Returns ``None`` on a concurrent duplicate.
    """
    now = datetime.now(UTC)
    stmt = (
        pg_insert(User)
        .values(
            id=uuid7(),
            idp_issuer=idp_issuer,
            idp_subject=idp_subject,
            email=email,
            email_verified=email_verified,
            display_name=display_name,
            status="active",
            created_at=now,
            updated_at=now,
            version=1,
        )
        .on_conflict_do_nothing(index_elements=["idp_issuer", "idp_subject"])
        .returning(User)
    )
    result = await session.execute(stmt)
    return result.scalars().one_or_none()


async def get_by_idp_identity(
    session: AsyncSession, *, idp_issuer: str, idp_subject: str
) -> User | None:
    stmt = select(User).where(User.idp_issuer == idp_issuer, User.idp_subject == idp_subject)
    return (await session.execute(stmt)).scalars().one_or_none()


async def get_by_id(session: AsyncSession, user_id: UUID) -> User | None:
    return await session.get(User, user_id)


async def list_by_ids(session: AsyncSession, ids: list[UUID]) -> list[User]:
    if not ids:
        return []
    stmt = select(User).where(User.id.in_(ids))
    return list((await session.execute(stmt)).scalars().all())


async def is_handle_taken(session: AsyncSession, handle_normalized: str) -> bool:
    if await session.get(ReservedHandle, handle_normalized) is not None:
        return True
    stmt = select(User.id).where(User.handle_normalized == handle_normalized)
    return (await session.execute(stmt)).first() is not None


async def update_profile(
    session: AsyncSession,
    user_id: UUID,
    *,
    expected_version: int,
    display_name: str | None = None,
    handle: str | None = None,
    handle_normalized: str | None = None,
) -> User:
    values: dict[str, object] = {"updated_at": datetime.now(UTC), "version": expected_version + 1}
    if display_name is not None:
        values["display_name"] = display_name
    if handle is not None:
        values["handle"] = handle
        values["handle_normalized"] = handle_normalized
    stmt = (
        update(User)
        .where(User.id == user_id, User.version == expected_version)
        .values(**values)
        .returning(User)
    )
    row = (await session.execute(stmt)).scalars().one_or_none()
    if row is None:
        raise VersionConflict(user_id)
    return row


async def disable_user(session: AsyncSession, user_id: UUID, *, reason: str) -> User | None:
    now = datetime.now(UTC)
    stmt = (
        update(User)
        .where(User.id == user_id, User.status == "active")
        .values(
            status="disabled",
            status_reason=reason,
            disabled_at=now,
            updated_at=now,
            version=User.version + 1,
        )
        .returning(User)
    )
    return (await session.execute(stmt)).scalars().one_or_none()


async def reactivate_user(session: AsyncSession, user_id: UUID) -> User | None:
    now = datetime.now(UTC)
    stmt = (
        update(User)
        .where(User.id == user_id, User.status == "disabled")
        .values(
            status="active",
            status_reason=None,
            disabled_at=None,
            updated_at=now,
            version=User.version + 1,
        )
        .returning(User)
    )
    return (await session.execute(stmt)).scalars().one_or_none()
