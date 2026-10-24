"""Concurrency tests against the local Compose Postgres (`make test-env`).
Skip if unreachable. LLD-07 §13: oversell, multi-SKU deadlock avoidance,
and the expiry-vs-commit race -- exercised at the ``service`` layer
directly (not through HTTP) so many concurrent calls can run without
routing them all through a synchronous TestClient.
"""

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4, uuid7

import pytest
import pytest_asyncio
from app import service
from app.models import Reservation, ReservationLine, StockItem
from sf_db import build_engine, build_session_factory
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def engine() -> AsyncIterator[AsyncEngine]:
    try:
        # A bigger pool than the request-handling default: this test opens
        # many concurrent transactions on purpose (that's what it's
        # measuring), and the pool itself shouldn't be the bottleneck.
        eng = build_engine(
            host=os.environ.get("SF_DB_HOST", "localhost"),
            port=int(os.environ.get("SF_DB_PORT", "5432")),
            database=os.environ["SF_DB_NAME"],
            user=os.environ.get("SF_DB_USER", "svc_inventory"),
            password=os.environ["SF_DB_PASSWORD"],
            pool_size=50,
            max_overflow=0,
            pool_timeout=30.0,
        )
        async with eng.connect():
            pass
    except Exception as exc:
        pytest.skip(f"local Postgres not reachable: {exc}")
    yield eng
    await eng.dispose()


@pytest.fixture
def session_factory(engine: AsyncEngine) -> async_sessionmaker:
    return build_session_factory(engine)


async def _seed_stock(
    session_factory: async_sessionmaker, *, org_id: UUID, sku_id: UUID, on_hand: int
) -> UUID:
    product_id = uuid7()
    async with session_factory() as session:
        session.add(
            StockItem(
                sku_id=sku_id,
                org_id=org_id,
                product_id=product_id,
                on_hand=on_hand,
                reserved=0,
                low_stock_threshold=0,
                version=1,
                updated_at=datetime.now(UTC),
            )
        )
        await session.commit()
    return product_id


async def test_oversell_exactly_stock_count_succeeds(session_factory: async_sessionmaker) -> None:
    org_id, sku_id = uuid4(), uuid7()
    await _seed_stock(session_factory, org_id=org_id, sku_id=sku_id, on_hand=50)
    order_ids = [uuid7() for _ in range(200)]

    outcomes = await asyncio.gather(
        *[
            service.reserve(
                session_factory,
                org_id=org_id,
                order_id=order_id,
                lines=[(sku_id, 1)],
                ttl_seconds=900,
            )
            for order_id in order_ids
        ]
    )

    reserved = [o for o in outcomes if o.shortfalls is None]
    rejected = [o for o in outcomes if o.shortfalls is not None]
    assert len(reserved) == 50
    assert len(rejected) == 150

    async with session_factory() as session:
        item = await session.get(StockItem, sku_id)
        assert item is not None
        assert item.reserved == 50
        assert item.on_hand == 50


async def test_multi_sku_orders_in_opposite_sku_order_dont_deadlock(
    session_factory: async_sessionmaker,
) -> None:
    org_id = uuid4()
    sku_a, sku_b = sorted([uuid7(), uuid7()])
    await _seed_stock(session_factory, org_id=org_id, sku_id=sku_a, on_hand=1000)
    # Re-seed with the same sku_id values but distinct product_id/org isn't
    # needed; just add sku_b's own row.
    product_id_b = uuid7()
    async with session_factory() as session:
        session.add(
            StockItem(
                sku_id=sku_b,
                org_id=org_id,
                product_id=product_id_b,
                on_hand=1000,
                reserved=0,
                low_stock_threshold=0,
                version=1,
                updated_at=datetime.now(UTC),
            )
        )
        await session.commit()

    # Half the orders list (sku_a, sku_b); half list (sku_b, sku_a) in the
    # request body -- service.py sorts by sku_id before locking either way,
    # so this must never deadlock (LLD-07 §5).
    orders_forward = [(uuid7(), [(sku_a, 1), (sku_b, 1)]) for _ in range(40)]
    orders_reverse = [(uuid7(), [(sku_b, 1), (sku_a, 1)]) for _ in range(40)]

    outcomes = await asyncio.gather(
        *[
            service.reserve(
                session_factory, org_id=org_id, order_id=oid, lines=lines, ttl_seconds=900
            )
            for oid, lines in orders_forward + orders_reverse
        ]
    )

    assert all(o.shortfalls is None for o in outcomes)
    async with session_factory() as session:
        item_a = await session.get(StockItem, sku_a)
        item_b = await session.get(StockItem, sku_b)
        assert item_a.reserved == 80
        assert item_b.reserved == 80


async def test_expiry_vs_commit_race_has_exactly_one_outcome(
    session_factory: async_sessionmaker,
) -> None:
    org_id, sku_id = uuid4(), uuid7()
    await _seed_stock(session_factory, org_id=org_id, sku_id=sku_id, on_hand=10)
    order_id = uuid7()
    outcome = await service.reserve(
        session_factory, org_id=org_id, order_id=order_id, lines=[(sku_id, 3)], ttl_seconds=900
    )
    assert outcome.shortfalls is None

    # Force it into the sweeper's claim window.
    async with session_factory() as session:
        reservation = await session.get(Reservation, outcome.reservation.id)
        assert reservation is not None
        reservation.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()

    commit_result, expire_count = await asyncio.gather(
        service.commit(session_factory, order_id=order_id),
        service.expire_batch(session_factory, limit=10),
        return_exceptions=True,
    )

    async with session_factory() as session:
        reservation = await session.get(Reservation, outcome.reservation.id)
        item = await session.get(StockItem, sku_id)
        lines = (
            (
                await session.execute(
                    select(ReservationLine).where(ReservationLine.reservation_id == reservation.id)
                )
            )
            .scalars()
            .all()
        )

    # Whichever won, the reservation lands in exactly one terminal state and
    # the ledger/stock stays consistent either way (LLD-07 §6).
    assert reservation.status in ("committed", "expired")
    if reservation.status == "committed":
        assert item.on_hand == 7
        assert item.reserved == 0
        assert not isinstance(commit_result, Exception)
    else:
        assert item.on_hand == 10
        assert item.reserved == 0
        assert isinstance(commit_result, service.ReservationNotActive)
    assert len(lines) == 1  # the reservation's own line is untouched either way
