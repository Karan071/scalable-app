"""orders-svc data access (LLD-06 §3.1, §5.2, §6)."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from .models import SAGA_TERMINAL_STATES, CheckoutSaga, Order, OrderLine, OrderStatusHistory

# --- orders -------------------------------------------------------------------


async def insert_order(session: AsyncSession, order: Order, lines: list[OrderLine]) -> Order:
    session.add(order)
    for line in lines:
        session.add(line)
    await session.flush()
    return order


async def get_order(session: AsyncSession, order_id: UUID) -> Order | None:
    return await session.get(Order, order_id)


async def get_order_for_customer(
    session: AsyncSession, order_id: UUID, *, customer_id: UUID
) -> Order | None:
    stmt = select(Order).where(Order.id == order_id, Order.customer_id == customer_id)
    return (await session.execute(stmt)).scalars().one_or_none()


async def get_order_for_org(session: AsyncSession, order_id: UUID, *, org_id: UUID) -> Order | None:
    stmt = select(Order).where(Order.id == order_id, Order.org_id == org_id)
    return (await session.execute(stmt)).scalars().one_or_none()


async def lock_order(session: AsyncSession, order_id: UUID) -> Order | None:
    stmt = select(Order).where(Order.id == order_id).with_for_update()
    return (await session.execute(stmt)).scalars().one_or_none()


async def list_order_lines(session: AsyncSession, order_id: UUID) -> list[OrderLine]:
    stmt = select(OrderLine).where(OrderLine.order_id == order_id).order_by(OrderLine.line_no)
    return list((await session.execute(stmt)).scalars().all())


async def list_order_status_history(
    session: AsyncSession, order_id: UUID
) -> list[OrderStatusHistory]:
    stmt = (
        select(OrderStatusHistory)
        .where(OrderStatusHistory.order_id == order_id)
        .order_by(OrderStatusHistory.at)
    )
    return list((await session.execute(stmt)).scalars().all())


async def list_orders_for_customer(
    session: AsyncSession,
    *,
    customer_id: UUID,
    org_id: UUID | None,
    cursor: UUID | None,
    limit: int,
) -> list[Order]:
    stmt = select(Order).where(Order.customer_id == customer_id)
    if org_id is not None:
        stmt = stmt.where(Order.org_id == org_id)
    if cursor is not None:
        # Keyset on (created_at DESC, id): the cursor carries the last id
        # seen; join back to its created_at to keep the page boundary exact.
        anchor = select(Order.created_at).where(Order.id == cursor).scalar_subquery()
        stmt = stmt.where(tuple_(Order.created_at, Order.id) < tuple_(anchor, cursor))
    stmt = stmt.order_by(Order.created_at.desc(), Order.id.desc()).limit(limit)
    return list((await session.execute(stmt)).scalars().all())


async def list_orders_for_org(
    session: AsyncSession,
    *,
    org_id: UUID,
    status: str | None,
    cursor: UUID | None,
    limit: int,
) -> list[Order]:
    stmt = select(Order).where(Order.org_id == org_id)
    if status is not None:
        stmt = stmt.where(Order.status == status)
    if cursor is not None:
        anchor = select(Order.created_at).where(Order.id == cursor).scalar_subquery()
        stmt = stmt.where(tuple_(Order.created_at, Order.id) < tuple_(anchor, cursor))
    stmt = stmt.order_by(Order.created_at.desc(), Order.id.desc()).limit(limit)
    return list((await session.execute(stmt)).scalars().all())


def apply_order_transition(
    session: AsyncSession,
    order: Order,
    *,
    to_status: str,
    actor_type: str,
    actor_id: str,
    reason: str | None = None,
    payment_status: str | None = None,
    cancel_reason: str | None = None,
) -> None:
    """Mutates an already-locked ``Order`` and appends its history row
    (LLD-06 §6) -- caller is inside an open transaction."""
    now = datetime.now(UTC)
    session.add(
        OrderStatusHistory(
            order_id=order.id,
            from_status=order.status,
            to_status=to_status,
            actor_type=actor_type,
            actor_id=actor_id,
            reason=reason,
            at=now,
        )
    )
    order.status = to_status
    if payment_status is not None:
        order.payment_status = payment_status
    if cancel_reason is not None:
        order.cancel_reason = cancel_reason
    order.version += 1
    order.updated_at = now


def set_payment_status(order: Order, payment_status: str) -> None:
    """A payment_status-only change (LLD-06 §3.1: "payment_status
    (projection)") -- no history row, since §6's history is specifically
    about the order's own ``status`` transitions."""
    order.payment_status = payment_status
    order.version += 1
    order.updated_at = datetime.now(UTC)


# --- checkout sagas -----------------------------------------------------------


async def insert_saga(
    session: AsyncSession, *, order_id: UUID, deadline_at: datetime
) -> CheckoutSaga:
    now = datetime.now(UTC)
    saga = CheckoutSaga(
        order_id=order_id,
        state="RESERVING",
        reservation_id=None,
        payment_id=None,
        attempts=0,
        # Due immediately: the recovery worker's first look at any
        # in-flight saga is a no-op unless it's actually stuck (see
        # service.recover_one), so there's no harm in it being eligible
        # from the start -- and it's how a crash right after this insert
        # still gets found.
        next_action_at=now,
        deadline_at=deadline_at,
        last_error=None,
        version=1,
        updated_at=now,
    )
    session.add(saga)
    await session.flush()
    return saga


async def get_saga(session: AsyncSession, order_id: UUID) -> CheckoutSaga | None:
    return await session.get(CheckoutSaga, order_id)


async def lock_saga(session: AsyncSession, order_id: UUID) -> CheckoutSaga | None:
    stmt = select(CheckoutSaga).where(CheckoutSaga.order_id == order_id).with_for_update()
    return (await session.execute(stmt)).scalars().one_or_none()


async def guarded_transition(
    session: AsyncSession,
    order_id: UUID,
    *,
    from_state: str,
    to_state: str,
    **fields: Any,
) -> bool:
    """LLD-06 §5.3: ``UPDATE checkout_sagas SET state=:to ... WHERE
    order_id=:id AND state=:from``. Zero rows means a duplicate or late
    event -- the caller logs/counts and moves on, never raises.

    Every non-terminal landing state gets ``next_action_at = now()`` by
    default (making it immediately eligible for the recovery worker's
    claim, which is a no-op unless it's actually stuck -- see
    ``service.recover_one``), and every terminal one gets ``None``. A
    caller that needs a specific backoff (e.g. ``AUTH_PENDING``'s retry
    delay) passes its own ``next_action_at`` in ``fields`` to override
    this.
    """
    if "next_action_at" not in fields:
        fields["next_action_at"] = None if to_state in SAGA_TERMINAL_STATES else datetime.now(UTC)
    stmt = (
        update(CheckoutSaga)
        .where(CheckoutSaga.order_id == order_id, CheckoutSaga.state == from_state)
        .values(
            state=to_state,
            version=CheckoutSaga.version + 1,
            updated_at=datetime.now(UTC),
            **fields,
        )
        .returning(CheckoutSaga.order_id)
    )
    return (await session.execute(stmt)).scalars().one_or_none() is not None


#: How long a claimed saga is leased to this tick before another replica's
#: concurrent claim may pick it up again -- covers the gap between this
#: claim transaction committing (releasing the row lock) and
#: ``service.recover_one``'s own transaction re-locking it.
CLAIM_LEASE_SECONDS = 10


async def claim_due_sagas(session: AsyncSession, *, limit: int) -> list[UUID]:
    """LLD-06 §5.3's recovery worker claim: ``SKIP LOCKED`` partitions the
    work across replicas with no leader election needed. Pushes
    ``next_action_at`` forward by ``CLAIM_LEASE_SECONDS`` *in this same
    transaction*, atomically with the claim -- otherwise a concurrent tick
    (this or another replica) could re-claim the same rows the instant
    this transaction commits and the row lock releases, before
    ``recover_one`` finishes."""
    now = datetime.now(UTC)
    stmt = (
        select(CheckoutSaga.order_id)
        .where(
            CheckoutSaga.state.not_in(("COMPLETED", "CANCELLED")),
            CheckoutSaga.next_action_at.is_not(None),
            CheckoutSaga.next_action_at <= now,
        )
        .order_by(CheckoutSaga.next_action_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    order_ids = list((await session.execute(stmt)).scalars().all())
    if order_ids:
        await session.execute(
            update(CheckoutSaga)
            .where(CheckoutSaga.order_id.in_(order_ids))
            .values(next_action_at=now + timedelta(seconds=CLAIM_LEASE_SECONDS))
        )
    return order_ids
