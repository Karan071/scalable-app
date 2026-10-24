"""inventory-svc business logic (LLD-07 §4-§7).

Reserve/commit/release/restock each own their whole transaction (opened
from a ``session_factory``, not handed an already-open ``AsyncSession``) so
``sf_db.retry_on_transient`` can re-run the entire operation from scratch on
a deadlock/serialization failure, per its own docstring.
"""

import json
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sf_db import retry_on_transient, transaction
from sf_messaging import add_to_outbox, build_envelope, record_audit
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from . import repository
from .models import Reservation, StockItem

_NOTIFY_CHANNEL = "outbox_inventory"
_EVENTS_EXCHANGE = "sf.events"

#: LLD-07 §4: "lock_timeout = 1s and statement_timeout = 2s" for every
#: reserve/commit/release/restock/expire transaction.
_TX_LOCK_TIMEOUT_SQL = text("SET LOCAL lock_timeout = '1s'")
_TX_STATEMENT_TIMEOUT_SQL = text("SET LOCAL statement_timeout = '2s'")


class ReservationNotFound(Exception):
    pass


class ReservationNotActive(Exception):
    """Commit/release attempted on an ``expired``/``released``/``rejected`` reservation."""


class ReservationCommitted(Exception):
    """Release attempted on a ``committed`` reservation; use restock instead."""


class StockItemNotFound(Exception):
    pass


class VersionConflict(Exception):
    pass


class BelowReserved(Exception):
    """An admin adjustment would push ``on_hand`` below ``reserved``."""


class WithinDeadline(Exception):
    """Manual admin release refused: the saga may still complete (LLD-07 §8)."""


class _InsufficientStock(Exception):
    def __init__(self, shortfalls: list[dict[str, object]]) -> None:
        self.shortfalls = shortfalls


Lines = list[tuple[UUID, int]]


def _merge_and_sort_lines(lines: Lines) -> Lines:
    """Duplicate SKU lines merge (sum qty); sorted ascending for the
    deterministic lock order LLD-07 §5 requires."""
    merged: OrderedDict[UUID, int] = OrderedDict()
    for sku_id, qty in lines:
        merged[sku_id] = merged.get(sku_id, 0) + qty
    return sorted(merged.items())


async def _set_tx_timeouts(session: AsyncSession) -> None:
    await session.execute(_TX_LOCK_TIMEOUT_SQL)
    await session.execute(_TX_STATEMENT_TIMEOUT_SQL)


async def _emit(
    session: AsyncSession,
    *,
    event_type: str,
    routing_key: str,
    aggregate_type: str,
    aggregate_id: str,
    aggregate_version: int,
    org_id: UUID,
    data: dict[str, object],
) -> None:
    envelope = build_envelope(
        event_type=event_type,
        source="inventory-svc",
        subject=aggregate_id,
        data=data,
        schema_version="1.0",
        aggregate_type=aggregate_type,
        aggregate_version=aggregate_version,
        org_id=str(org_id),
    )
    await add_to_outbox(
        session,
        envelope,
        exchange=_EVENTS_EXCHANGE,
        routing_key=routing_key,
        aggregate_id=aggregate_id,
        notify_channel=_NOTIFY_CHANNEL,
    )


def _availability_flags(stock_item: StockItem) -> tuple[bool, bool]:
    available = stock_item.on_hand - stock_item.reserved
    return available > 0, available <= stock_item.low_stock_threshold


async def _emit_availability_changed_if_flipped(
    session: AsyncSession, stock_item: StockItem, before_flags: tuple[bool, bool]
) -> None:
    """LLD-07 §7: emitted *only* when ``in_stock``/``low_stock`` flips, so
    event volume tracks state changes, not order volume."""
    after_flags = _availability_flags(stock_item)
    if after_flags == before_flags:
        return
    in_stock, low_stock = after_flags
    await _emit(
        session,
        event_type="inventory.availability_changed",
        routing_key="inventory.availability_changed.v1",
        aggregate_type="stock_item",
        aggregate_id=str(stock_item.sku_id),
        aggregate_version=stock_item.version,
        org_id=stock_item.org_id,
        data={
            "org_id": str(stock_item.org_id),
            "product_id": str(stock_item.product_id),
            "sku_id": str(stock_item.sku_id),
            "in_stock": in_stock,
            "low_stock": low_stock,
            "version": stock_item.version,
        },
    )


# --- reserve (LLD-07 §4.1) ---------------------------------------------------


@dataclass(frozen=True)
class ReserveOutcome:
    reservation: Reservation
    shortfalls: list[dict[str, object]] | None  # non-None only when status == "rejected"


@retry_on_transient()
async def _attempt_reserve(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    org_id: UUID,
    order_id: UUID,
    lines: Lines,
    ttl_seconds: int,
    on_behalf_of: UUID | None,
) -> Reservation:
    async with transaction(session_factory) as session:
        await _set_tx_timeouts(session)

        existing = await repository.get_reservation_by_order(session, order_id)
        if existing is not None:
            return existing  # idempotent replay (LLD-07 §4.1)

        merged = _merge_and_sort_lines(lines)
        sku_ids = [sku_id for sku_id, _qty in merged]
        stock = await repository.lock_stock_items(session, org_id=org_id, sku_ids=sku_ids)

        shortfalls: list[dict[str, object]] = []
        for sku_id, qty in merged:
            item = stock.get(sku_id)
            available = (item.on_hand - item.reserved) if item is not None else 0
            if item is None or available < qty:
                shortfalls.append(
                    {"sku_id": str(sku_id), "requested": qty, "available": max(available, 0)}
                )
        if shortfalls:
            raise _InsufficientStock(shortfalls)

        reservation = await repository.insert_active_reservation(
            session, org_id=org_id, order_id=order_id, lines=merged, ttl_seconds=ttl_seconds
        )
        for sku_id, qty in merged:
            item = stock[sku_id]
            before_flags = _availability_flags(item)
            await repository.apply_stock_delta(session, item, delta_on_hand=0, delta_reserved=qty)
            await repository.insert_movement(
                session,
                org_id=org_id,
                sku_id=sku_id,
                movement_type="reserve",
                delta_on_hand=0,
                delta_reserved=qty,
                ref_type="order",
                ref_id=str(order_id),
                actor_id=on_behalf_of,
            )
            await _emit_availability_changed_if_flipped(session, item, before_flags)

        await _emit(
            session,
            event_type="inventory.reserved",
            routing_key="inventory.reserved.v1",
            aggregate_type="reservation",
            aggregate_id=str(reservation.id),
            aggregate_version=1,
            org_id=org_id,
            data={
                "org_id": str(org_id),
                "order_id": str(order_id),
                "reservation_id": str(reservation.id),
                "lines": [{"sku_id": str(s), "qty": q} for s, q in merged],
                "expires_at": reservation.expires_at.isoformat(),
            },
        )
        return reservation


async def reserve(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    org_id: UUID,
    order_id: UUID,
    lines: Lines,
    ttl_seconds: int,
    on_behalf_of: UUID | None = None,
) -> ReserveOutcome:
    try:
        reservation = await _attempt_reserve(
            session_factory,
            org_id=org_id,
            order_id=order_id,
            lines=lines,
            ttl_seconds=ttl_seconds,
            on_behalf_of=on_behalf_of,
        )
    except _InsufficientStock as exc:
        async with transaction(session_factory) as session:
            await repository.insert_rejected_reservation(
                session, org_id=org_id, order_id=order_id, shortfalls=exc.shortfalls
            )
            # ON CONFLICT DO NOTHING above: if a concurrent attempt for this
            # same order_id won the race and actually reserved, this reads
            # that real outcome back instead of assuming a rejection.
            landed = await repository.get_reservation_by_order(session, order_id)
        assert landed is not None
        if landed.status != "rejected":
            return ReserveOutcome(reservation=landed, shortfalls=None)
        shortfalls = json.loads(landed.reason) if landed.reason else exc.shortfalls
        return ReserveOutcome(reservation=landed, shortfalls=shortfalls)

    if reservation.status == "rejected":
        shortfalls = json.loads(reservation.reason) if reservation.reason else []
        return ReserveOutcome(reservation=reservation, shortfalls=shortfalls)
    return ReserveOutcome(reservation=reservation, shortfalls=None)


# --- commit (LLD-07 §4.2) ----------------------------------------------------


@retry_on_transient()
async def commit(
    session_factory: async_sessionmaker[AsyncSession], *, order_id: UUID
) -> Reservation:
    async with transaction(session_factory) as session:
        await _set_tx_timeouts(session)

        reservation = await repository.lock_reservation_by_order(session, order_id)
        if reservation is None:
            raise ReservationNotFound(order_id)
        if reservation.status == "committed":
            return reservation  # idempotent no-op
        if reservation.status != "active":
            raise ReservationNotActive(reservation.status)

        lines = await repository.list_reservation_lines(session, reservation.id)
        sku_ids = sorted(line.sku_id for line in lines)
        stock = await repository.lock_stock_items(
            session, org_id=reservation.org_id, sku_ids=sku_ids
        )
        for line in sorted(lines, key=lambda ln: ln.sku_id):
            item = stock[line.sku_id]
            before_flags = _availability_flags(item)
            await repository.apply_stock_delta(
                session, item, delta_on_hand=-line.qty, delta_reserved=-line.qty
            )
            await repository.insert_movement(
                session,
                org_id=reservation.org_id,
                sku_id=line.sku_id,
                movement_type="commit",
                delta_on_hand=-line.qty,
                delta_reserved=-line.qty,
                ref_type="order",
                ref_id=str(order_id),
                actor_id=None,
            )
            # on_hand and reserved both drop by qty, so "available" never
            # moves -- this call never flips in_stock/low_stock, but it's
            # cheap and keeps commit symmetric with every other mutator.
            await _emit_availability_changed_if_flipped(session, item, before_flags)

        repository.mark_reservation(reservation, status="committed")
        await _emit(
            session,
            event_type="inventory.committed",
            routing_key="inventory.committed.v1",
            aggregate_type="reservation",
            aggregate_id=str(reservation.id),
            aggregate_version=2,
            org_id=reservation.org_id,
            data={
                "org_id": str(reservation.org_id),
                "order_id": str(order_id),
                "reservation_id": str(reservation.id),
            },
        )
        return reservation


# --- release (LLD-07 §4.3) ---------------------------------------------------


@retry_on_transient()
async def release(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    org_id: UUID,
    order_id: UUID,
    reason: str | None = None,
) -> Reservation:
    async with transaction(session_factory) as session:
        await _set_tx_timeouts(session)

        reservation = await repository.lock_reservation_by_order(session, order_id)
        if reservation is None:
            # LLD-07 §4.3: a release for an unknown order leaves a tombstone
            # so a late reserve for that order is refused.
            await repository.insert_tombstone_reservation(
                session, org_id=org_id, order_id=order_id, reason=reason
            )
            tombstone = await repository.get_reservation_by_order(session, order_id)
            assert tombstone is not None
            return tombstone
        if reservation.status in ("released", "expired", "rejected"):
            return reservation  # idempotent no-op
        if reservation.status == "committed":
            raise ReservationCommitted(reservation.status)

        lines = await repository.list_reservation_lines(session, reservation.id)
        sku_ids = sorted(line.sku_id for line in lines)
        stock = await repository.lock_stock_items(
            session, org_id=reservation.org_id, sku_ids=sku_ids
        )
        for line in sorted(lines, key=lambda ln: ln.sku_id):
            item = stock[line.sku_id]
            before_flags = _availability_flags(item)
            await repository.apply_stock_delta(
                session, item, delta_on_hand=0, delta_reserved=-line.qty
            )
            await repository.insert_movement(
                session,
                org_id=reservation.org_id,
                sku_id=line.sku_id,
                movement_type="release",
                delta_on_hand=0,
                delta_reserved=-line.qty,
                ref_type="order",
                ref_id=str(order_id),
                actor_id=None,
            )
            await _emit_availability_changed_if_flipped(session, item, before_flags)

        repository.mark_reservation(reservation, status="released", reason=reason)
        await _emit(
            session,
            event_type="inventory.released",
            routing_key="inventory.released.v1",
            aggregate_type="reservation",
            aggregate_id=str(reservation.id),
            aggregate_version=2,
            org_id=reservation.org_id,
            data={
                "org_id": str(reservation.org_id),
                "order_id": str(order_id),
                "reservation_id": str(reservation.id),
                "reason": reason,
            },
        )
        return reservation


async def manual_release(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    org_id: UUID,
    reservation_id: UUID,
    reason: str,
    force: bool,
) -> Reservation:
    """LLD-07 §8: an admin-triggered release, refused while the saga may
    still complete unless ``force`` is set with a reason."""
    async with transaction(session_factory) as session:
        reservation = await repository.get_reservation(
            session, org_id=org_id, reservation_id=reservation_id
        )
    if reservation is None:
        raise ReservationNotFound(reservation_id)
    if reservation.status != "active":
        raise ReservationNotActive(reservation.status)
    if not force and reservation.expires_at > datetime.now(UTC):
        raise WithinDeadline(reservation.expires_at)
    return await release(
        session_factory, org_id=org_id, order_id=reservation.order_id, reason=reason
    )


# --- restock (LLD-07 §4.4) ---------------------------------------------------


@retry_on_transient()
async def restock(
    session_factory: async_sessionmaker[AsyncSession], *, org_id: UUID, order_id: UUID, lines: Lines
) -> None:
    async with transaction(session_factory) as session:
        await _set_tx_timeouts(session)

        merged = _merge_and_sort_lines(lines)
        sku_ids = [sku_id for sku_id, _qty in merged]
        stock = await repository.lock_stock_items(session, org_id=org_id, sku_ids=sku_ids)

        applied = False
        for sku_id, qty in merged:
            item = stock.get(sku_id)
            if item is None:
                continue
            before_flags = _availability_flags(item)
            # INV-5: a replayed restock command for the same order+sku is a
            # no-op, not a double credit.
            inserted = await repository.insert_movement(
                session,
                org_id=org_id,
                sku_id=sku_id,
                movement_type="restock",
                delta_on_hand=qty,
                delta_reserved=0,
                ref_type="order",
                ref_id=str(order_id),
                actor_id=None,
            )
            if not inserted:
                continue
            await repository.apply_stock_delta(session, item, delta_on_hand=qty, delta_reserved=0)
            await _emit_availability_changed_if_flipped(session, item, before_flags)
            applied = True

        if applied:
            await _emit(
                session,
                event_type="inventory.restocked",
                routing_key="inventory.restocked.v1",
                aggregate_type="order",
                aggregate_id=str(order_id),
                aggregate_version=1,
                org_id=org_id,
                data={
                    "org_id": str(org_id),
                    "order_id": str(order_id),
                    "lines": [{"sku_id": str(s), "qty": q} for s, q in merged],
                },
            )


# --- admin adjustment (LLD-07 §4.5) -----------------------------------------


async def adjust_stock(
    session: AsyncSession,
    *,
    org_id: UUID,
    sku_id: UUID,
    actor_id: UUID,
    adjustment_id: UUID,
    delta: int | None,
    set_on_hand: int | None,
    reason: str,
    expected_version: int,
) -> StockItem:
    """Optimistic lock, low contention (LLD-07 §4.5) -- no row lock needed."""
    existing_movement = await repository.get_movement_by_ref(
        session,
        ref_type="adjustment",
        ref_id=str(adjustment_id),
        movement_type="adjust",
        sku_id=sku_id,
    )
    if existing_movement is not None:
        item = await repository.get_stock_item(session, org_id=org_id, sku_id=sku_id)
        assert item is not None
        return item  # idempotent replay (INV-5)

    current = await repository.get_stock_item(session, org_id=org_id, sku_id=sku_id)
    if current is None:
        raise StockItemNotFound(sku_id)

    new_on_hand = set_on_hand if set_on_hand is not None else current.on_hand + (delta or 0)
    if new_on_hand < current.reserved:
        raise BelowReserved(sku_id)

    before_flags = _availability_flags(current)
    updated = await repository.adjust_stock_row(
        session,
        org_id=org_id,
        sku_id=sku_id,
        expected_version=expected_version,
        new_on_hand=new_on_hand,
    )
    if updated is None:
        raise VersionConflict(sku_id)

    await repository.insert_movement(
        session,
        org_id=org_id,
        sku_id=sku_id,
        movement_type="adjust",
        delta_on_hand=new_on_hand - current.on_hand,
        delta_reserved=0,
        ref_type="adjustment",
        ref_id=str(adjustment_id),
        actor_id=actor_id,
    )
    await record_audit(
        session,
        notify_channel=_NOTIFY_CHANNEL,
        source_service="inventory-svc",
        action="stock.adjusted",
        actor_type="user",
        actor_id=str(actor_id),
        domain=f"org:{org_id}",
        target_type="stock_item",
        target_id=str(sku_id),
        aggregate_version=updated.version,
        reason=reason,
        changes={"on_hand": {"from": current.on_hand, "to": new_on_hand}},
    )
    await _emit_availability_changed_if_flipped(session, updated, before_flags)
    return updated


# --- expiry sweeper (LLD-07 §6) ---------------------------------------------


@retry_on_transient()
async def expire_batch(session_factory: async_sessionmaker[AsyncSession], *, limit: int) -> int:
    async with transaction(session_factory) as session:
        await _set_tx_timeouts(session)

        ids = await repository.select_expired_active_reservation_ids(session, limit=limit)
        if not ids:
            return 0

        expired_count = 0
        for reservation_id in ids:
            reservation = await session.get(Reservation, reservation_id)
            assert reservation is not None
            if reservation.status != "active":
                # Lost the race to a concurrent commit/release (LLD-07 §6):
                # the winner already moved it off 'active'. Nothing to do.
                continue

            lines = await repository.list_reservation_lines(session, reservation.id)
            sku_ids = sorted(line.sku_id for line in lines)
            stock = await repository.lock_stock_items(
                session, org_id=reservation.org_id, sku_ids=sku_ids
            )
            for line in sorted(lines, key=lambda ln: ln.sku_id):
                item = stock[line.sku_id]
                before_flags = _availability_flags(item)
                await repository.apply_stock_delta(
                    session, item, delta_on_hand=0, delta_reserved=-line.qty
                )
                await repository.insert_movement(
                    session,
                    org_id=reservation.org_id,
                    sku_id=line.sku_id,
                    movement_type="expire",
                    delta_on_hand=0,
                    delta_reserved=-line.qty,
                    ref_type="order",
                    ref_id=str(reservation.order_id),
                    actor_id=None,
                )
                await _emit_availability_changed_if_flipped(session, item, before_flags)

            repository.mark_reservation(reservation, status="expired")
            await _emit(
                session,
                event_type="inventory.reservation_expired",
                routing_key="inventory.reservation_expired.v1",
                aggregate_type="reservation",
                aggregate_id=str(reservation.id),
                aggregate_version=2,
                org_id=reservation.org_id,
                data={
                    "org_id": str(reservation.org_id),
                    "order_id": str(reservation.order_id),
                    "reservation_id": str(reservation.id),
                },
            )
            expired_count += 1
        return expired_count


# --- reconciliation (LLD-07 §9) ---------------------------------------------


async def run_reconciliation(
    session_factory: async_sessionmaker[AsyncSession],
) -> list[dict[str, object]]:
    """Returns every ledger/current mismatch found; the caller (the CLI job)
    reports and alerts, never auto-corrects (LLD-07 §9)."""
    async with transaction(session_factory) as session:
        return await repository.find_ledger_mismatches(session)
