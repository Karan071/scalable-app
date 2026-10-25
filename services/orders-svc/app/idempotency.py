"""The generic idempotency-key algorithm (LLD-06 §4), shared by checkout,
order cancellation, and refund. ``scope`` discriminates which write a
(principal, key) pair belongs to.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sf_db import transaction
from sqlalchemy import Select, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .models import IdempotencyKey

#: "locked_until (lease for the owner, now() + 15s, renewed by long steps)".
LEASE_SECONDS = 15
EXPIRES_HOURS = 24


def canonical_hash(payload: dict[str, Any]) -> str:
    """SHA-256 of canonical JSON (sorted keys) -- order-independent, so the
    same logical request always hashes the same way."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class Replay:
    status_code: int
    body: dict[str, Any]


@dataclass(frozen=True)
class InProgress:
    pass


@dataclass(frozen=True)
class KeyReused:
    pass


@dataclass(frozen=True)
class Claimed:
    """``resource_id`` is ``None`` for a fresh claim, or the previous
    owner's in-flight resource (e.g. ``order_id``) when taking over a
    lease after a crash -- the caller must not re-create that resource."""

    resource_id: UUID | None


ClaimResult = Replay | InProgress | KeyReused | Claimed


def _find_stmt(*, scope: str, principal_id: UUID, key: UUID) -> Select[IdempotencyKey]:
    return (
        select(IdempotencyKey)
        .where(
            IdempotencyKey.scope == scope,
            IdempotencyKey.principal_id == principal_id,
            IdempotencyKey.key == key,
        )
        .with_for_update()
    )


async def claim(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    scope: str,
    principal_id: UUID,
    key: UUID,
    request_hash: str,
) -> ClaimResult:
    now = datetime.now(UTC)
    async with transaction(session_factory) as session:
        insert_stmt = (
            pg_insert(IdempotencyKey)
            .values(
                scope=scope,
                principal_id=principal_id,
                key=key,
                request_hash=request_hash,
                status="in_progress",
                locked_until=now + timedelta(seconds=LEASE_SECONDS),
                resource_id=None,
                response_status=None,
                response_body=None,
                created_at=now,
                expires_at=now + timedelta(hours=EXPIRES_HOURS),
            )
            .on_conflict_do_nothing(index_elements=["scope", "principal_id", "key"])
            .returning(IdempotencyKey.key)
        )
        inserted = (await session.execute(insert_stmt)).scalars().one_or_none()
        if inserted is not None:
            return Claimed(resource_id=None)

        existing = (
            (await session.execute(_find_stmt(scope=scope, principal_id=principal_id, key=key)))
            .scalars()
            .one()
        )
        if existing.request_hash != request_hash:
            return KeyReused()
        if existing.status == "completed":
            assert existing.response_status is not None
            return Replay(status_code=existing.response_status, body=existing.response_body or {})
        if existing.locked_until > now:
            return InProgress()
        # The owner crashed (stale lease): take it over, keep the existing
        # resource_id so a resumed checkout never re-creates the order.
        existing.locked_until = now + timedelta(seconds=LEASE_SECONDS)
        return Claimed(resource_id=existing.resource_id)


async def attach_resource(
    session: AsyncSession, *, scope: str, principal_id: UUID, key: UUID, resource_id: UUID
) -> None:
    """Records the resource (e.g. ``order_id``) as soon as it's created, in
    the *same* transaction that creates it -- so a crash right after still
    lets a resumed owner find it via ``Claimed.resource_id``."""
    stmt = (
        update(IdempotencyKey)
        .where(
            IdempotencyKey.scope == scope,
            IdempotencyKey.principal_id == principal_id,
            IdempotencyKey.key == key,
        )
        .values(resource_id=resource_id)
    )
    await session.execute(stmt)


async def complete(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    scope: str,
    principal_id: UUID,
    key: UUID,
    status_code: int,
    body: dict[str, Any],
) -> None:
    async with transaction(session_factory) as session:
        stmt = (
            update(IdempotencyKey)
            .where(
                IdempotencyKey.scope == scope,
                IdempotencyKey.principal_id == principal_id,
                IdempotencyKey.key == key,
            )
            .values(status="completed", response_status=status_code, response_body=body)
        )
        await session.execute(stmt)
