"""inventory-svc data access (LLD-07 §3-§6).

Row locks are always taken in ascending ``sku_id`` order (deadlock
avoidance, LLD-07 §5) -- every caller here that locks more than one SKU
sorts first; functions that take a single id don't need to.
"""

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid7

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Reservation, ReservationLine, StockItem, StockMovement

#: LLD-07 §13: "At most 100" for the public availability lookup.
MAX_AVAILABILITY_SKUS = 100


# --- stock items -------------------------------------------------------------


async def get_stock_item(session: AsyncSession, *, org_id: UUID, sku_id: UUID) -> StockItem | None:
    stmt = select(StockItem).where(StockItem.org_id == org_id, StockItem.sku_id == sku_id)
    return (await session.execute(stmt)).scalars().one_or_none()


async def get_stock_items(
    session: AsyncSession, *, org_id: UUID, sku_ids: Sequence[UUID]
) -> dict[UUID, StockItem]:
    """Plain read, no lock (LLD-07 §5: availability reads tolerate staleness)."""
    stmt = select(StockItem).where(StockItem.org_id == org_id, StockItem.sku_id.in_(sku_ids))
    rows = (await session.execute(stmt)).scalars().all()
    return {row.sku_id: row for row in rows}


async def lock_stock_items(
    session: AsyncSession, *, org_id: UUID, sku_ids: Sequence[UUID]
) -> dict[UUID, StockItem]:
    """Pessimistic, ordered (LLD-07 §4.1, §5): ``ORDER BY sku_id`` fixes the
    lock-acquisition order across every multi-SKU operation, so two orders
    for the same SKUs can never deadlock against each other."""
    stmt = (
        select(StockItem)
        .where(StockItem.org_id == org_id, StockItem.sku_id.in_(sku_ids))
        .order_by(StockItem.sku_id)
        .with_for_update()
    )
    rows = (await session.execute(stmt)).scalars().all()
    return {row.sku_id: row for row in rows}


async def apply_stock_delta(
    session: AsyncSession, stock_item: StockItem, *, delta_on_hand: int, delta_reserved: int
) -> None:
    """Mutates an already-locked ``StockItem`` (caller holds the row lock from
    ``lock_stock_items``/``lock_stock_item``). INV-3's ``CHECK`` is the
    backstop if a logic bug ever lets this go negative or over-allocate."""
    stock_item.on_hand += delta_on_hand
    stock_item.reserved += delta_reserved
    stock_item.version += 1
    stock_item.updated_at = datetime.now(UTC)


async def upsert_new_stock_item(
    session: AsyncSession, *, sku_id: UUID, org_id: UUID, product_id: UUID
) -> None:
    """LLD-07 §3: a new SKU gets a zero-stock row on ``product.updated.v1``.
    ``ON CONFLICT DO NOTHING`` makes re-applying the same snapshot (replay,
    or an update to an existing SKU) a no-op here -- an admin's real stock
    count is never clobbered by a later catalog edit."""
    now = datetime.now(UTC)
    stmt = (
        pg_insert(StockItem)
        .values(
            sku_id=sku_id,
            org_id=org_id,
            product_id=product_id,
            on_hand=0,
            reserved=0,
            low_stock_threshold=0,
            version=1,
            updated_at=now,
        )
        .on_conflict_do_nothing(index_elements=["sku_id"])
    )
    await session.execute(stmt)


async def adjust_stock_row(
    session: AsyncSession,
    *,
    org_id: UUID,
    sku_id: UUID,
    expected_version: int,
    new_on_hand: int,
) -> StockItem | None:
    """Optimistic lock (LLD-07 §4.5): ``None`` means a version mismatch (412)
    or an unknown SKU (404) -- the caller tells those apart with a lookup."""
    stmt = (
        update(StockItem)
        .where(
            StockItem.org_id == org_id,
            StockItem.sku_id == sku_id,
            StockItem.version == expected_version,
        )
        .values(on_hand=new_on_hand, version=StockItem.version + 1, updated_at=datetime.now(UTC))
        .returning(StockItem)
    )
    return (await session.execute(stmt)).scalars().one_or_none()


async def list_stock_items(
    session: AsyncSession,
    *,
    org_id: UUID,
    low_stock: bool | None,
    cursor: UUID | None,
    limit: int,
) -> list[StockItem]:
    stmt = select(StockItem).where(StockItem.org_id == org_id)
    if low_stock:
        stmt = stmt.where(StockItem.on_hand - StockItem.reserved <= StockItem.low_stock_threshold)
    if cursor is not None:
        stmt = stmt.where(StockItem.sku_id > cursor)
    stmt = stmt.order_by(StockItem.sku_id).limit(limit)
    return list((await session.execute(stmt)).scalars().all())


# --- stock movements (ledger) ------------------------------------------------


async def insert_movement(
    session: AsyncSession,
    *,
    org_id: UUID,
    sku_id: UUID,
    movement_type: str,
    delta_on_hand: int,
    delta_reserved: int,
    ref_type: str,
    ref_id: str,
    actor_id: UUID | None,
) -> bool:
    """INV-5: ``ON CONFLICT DO NOTHING`` on ``(ref_type, ref_id, movement_type,
    sku_id)`` is what makes every operation in LLD-07 §4 safe to replay --
    returns whether this call actually inserted a new row (``False`` means
    "already applied, this is a replay")."""
    stmt = (
        pg_insert(StockMovement)
        .values(
            org_id=org_id,
            sku_id=sku_id,
            movement_type=movement_type,
            delta_on_hand=delta_on_hand,
            delta_reserved=delta_reserved,
            ref_type=ref_type,
            ref_id=ref_id,
            actor_id=actor_id,
            created_at=datetime.now(UTC),
        )
        .on_conflict_do_nothing(index_elements=["ref_type", "ref_id", "movement_type", "sku_id"])
        .returning(StockMovement.id)
    )
    inserted = (await session.execute(stmt)).scalars().one_or_none()
    return inserted is not None


async def get_movement_by_ref(
    session: AsyncSession, *, ref_type: str, ref_id: str, movement_type: str, sku_id: UUID
) -> StockMovement | None:
    stmt = select(StockMovement).where(
        StockMovement.ref_type == ref_type,
        StockMovement.ref_id == ref_id,
        StockMovement.movement_type == movement_type,
        StockMovement.sku_id == sku_id,
    )
    return (await session.execute(stmt)).scalars().one_or_none()


# --- reservations -------------------------------------------------------------


async def get_reservation_by_order(session: AsyncSession, order_id: UUID) -> Reservation | None:
    stmt = select(Reservation).where(Reservation.order_id == order_id)
    return (await session.execute(stmt)).scalars().one_or_none()


async def lock_reservation_by_order(session: AsyncSession, order_id: UUID) -> Reservation | None:
    stmt = select(Reservation).where(Reservation.order_id == order_id).with_for_update()
    return (await session.execute(stmt)).scalars().one_or_none()


async def get_reservation(
    session: AsyncSession, *, org_id: UUID, reservation_id: UUID
) -> Reservation | None:
    stmt = select(Reservation).where(Reservation.org_id == org_id, Reservation.id == reservation_id)
    return (await session.execute(stmt)).scalars().one_or_none()


async def list_reservation_lines(
    session: AsyncSession, reservation_id: UUID
) -> list[ReservationLine]:
    stmt = (
        select(ReservationLine)
        .where(ReservationLine.reservation_id == reservation_id)
        .order_by(ReservationLine.sku_id)
    )
    return list((await session.execute(stmt)).scalars().all())


async def list_reservations(
    session: AsyncSession,
    *,
    org_id: UUID,
    status: str | None,
    older_than: datetime | None,
    cursor: UUID | None,
    limit: int,
) -> list[Reservation]:
    stmt = select(Reservation).where(Reservation.org_id == org_id)
    if status is not None:
        stmt = stmt.where(Reservation.status == status)
    if older_than is not None:
        stmt = stmt.where(Reservation.created_at < older_than)
    if cursor is not None:
        stmt = stmt.where(Reservation.id > cursor)
    stmt = stmt.order_by(Reservation.id).limit(limit)
    return list((await session.execute(stmt)).scalars().all())


async def insert_active_reservation(
    session: AsyncSession,
    *,
    org_id: UUID,
    order_id: UUID,
    lines: Sequence[tuple[UUID, int]],
    ttl_seconds: int,
) -> Reservation:
    now = datetime.now(UTC)
    reservation = Reservation(
        id=uuid7(),
        org_id=org_id,
        order_id=order_id,
        status="active",
        expires_at=now + timedelta(seconds=ttl_seconds),
        reason=None,
        created_at=now,
        updated_at=now,
    )
    session.add(reservation)
    for sku_id, qty in lines:
        session.add(ReservationLine(reservation_id=reservation.id, sku_id=sku_id, qty=qty))
    await session.flush()
    return reservation


async def insert_rejected_reservation(
    session: AsyncSession,
    *,
    org_id: UUID,
    order_id: UUID,
    shortfalls: list[dict[str, object]],
) -> None:
    """LLD-07 §4.1: "record rejection in a separate short tx ... ON CONFLICT
    DO NOTHING" -- makes reserve deterministic per order even if a
    concurrent call already recorded the same rejection."""
    now = datetime.now(UTC)
    stmt = (
        pg_insert(Reservation)
        .values(
            id=uuid7(),
            org_id=org_id,
            order_id=order_id,
            status="rejected",
            expires_at=now,
            reason=json.dumps(shortfalls),
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_nothing(index_elements=["order_id"])
    )
    await session.execute(stmt)


async def insert_tombstone_reservation(
    session: AsyncSession, *, org_id: UUID, order_id: UUID, reason: str | None
) -> None:
    """LLD-07 §4.3: a release for an order with no reservation at all leaves
    a ``released`` tombstone, so a late reserve for that order is refused
    rather than racing a reservation into existence after release."""
    now = datetime.now(UTC)
    stmt = (
        pg_insert(Reservation)
        .values(
            id=uuid7(),
            org_id=org_id,
            order_id=order_id,
            status="released",
            expires_at=now,
            reason=reason,
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_nothing(index_elements=["order_id"])
    )
    await session.execute(stmt)


# --- reconciliation (LLD-07 §9, INV-6) ---------------------------------------


async def find_ledger_mismatches(session: AsyncSession) -> list[dict[str, object]]:
    """INV-6: ``on_hand``/``reserved`` must equal the ledger's own sums.
    Flags mismatches; never auto-corrects (LLD-07 §9) -- a mismatch means a
    bug, and silently "fixing" it would hide that."""
    ledger_sums = (
        select(
            StockMovement.sku_id,
            func.sum(StockMovement.delta_on_hand).label("on_hand_sum"),
            func.sum(StockMovement.delta_reserved).label("reserved_sum"),
        )
        .group_by(StockMovement.sku_id)
        .subquery()
    )
    stmt = (
        select(
            StockItem.sku_id,
            StockItem.org_id,
            StockItem.on_hand,
            StockItem.reserved,
            func.coalesce(ledger_sums.c.on_hand_sum, 0).label("ledger_on_hand"),
            func.coalesce(ledger_sums.c.reserved_sum, 0).label("ledger_reserved"),
        )
        .select_from(StockItem)
        .outerjoin(ledger_sums, ledger_sums.c.sku_id == StockItem.sku_id)
        .where(
            (StockItem.on_hand != func.coalesce(ledger_sums.c.on_hand_sum, 0))
            | (StockItem.reserved != func.coalesce(ledger_sums.c.reserved_sum, 0))
        )
    )
    rows = (await session.execute(stmt)).mappings().all()
    return [dict(row) for row in rows]


def mark_reservation(reservation: Reservation, *, status: str, reason: str | None = None) -> None:
    reservation.status = status
    if reason is not None:
        reservation.reason = reason
    reservation.updated_at = datetime.now(UTC)


async def select_expired_active_reservation_ids(session: AsyncSession, *, limit: int) -> list[UUID]:
    """LLD-07 §6's sweeper claim: ``SKIP LOCKED`` so one replica's batch
    never blocks on a row another replica (or a concurrent commit/release)
    already holds -- it just leaves that row for the next tick."""
    stmt = (
        select(Reservation.id)
        .where(Reservation.status == "active", Reservation.expires_at < datetime.now(UTC))
        .order_by(Reservation.expires_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    return list((await session.execute(stmt)).scalars().all())
