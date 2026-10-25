"""Integration tests against the local Compose Postgres (`make test-env`).
Skip if unreachable. LLD-06 §5.1's async continuation past ``COMMITTING``
-- the part that really runs today, since inventory-svc exists -- plus the
payment.* handlers, written against §7's events even though payments-svc
doesn't exist yet (E3.6) to call them.
"""

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4, uuid7

import pytest
import pytest_asyncio
from app import service
from app.models import CheckoutSaga, Order, OrderLine
from fakes import FakeInventoryClient
from sf_db import build_session_factory, transaction
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def engine() -> AsyncIterator[AsyncEngine]:
    try:
        eng = create_async_engine(
            f"postgresql+asyncpg://svc_orders:{os.environ['SF_DB_PASSWORD']}"
            f"@{os.environ['SF_DB_HOST']}:{os.environ['SF_DB_PORT']}/{os.environ['SF_DB_NAME']}"
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


async def _seed(
    session_factory: async_sessionmaker,
    *,
    state: str,
    status: str = "pending",
    payment_status: str = "authorized",
) -> UUID:
    org_id, customer_id, sku_id, order_id = uuid4(), uuid7(), uuid7(), uuid7()
    now = datetime.now(UTC)
    async with transaction(session_factory) as session:
        session.add(
            Order(
                id=order_id,
                org_id=org_id,
                customer_id=customer_id,
                status=status,
                payment_status=payment_status,
                cart_id=uuid7(),
                cart_version=1,
                idempotency_key=uuid7(),
                currency="EUR",
                subtotal_minor=1000,
                discount_minor=0,
                shipping_minor=500,
                tax_minor=0,
                total_minor=1500,
                promo_code=None,
                pricing_fingerprint="fake",
                shipping_address={
                    "name": "A",
                    "line1": "1 Main St",
                    "city": "Berlin",
                    "postal_code": "10115",
                    "country": "DE",
                },
                cancel_reason=None,
                version=1,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            OrderLine(
                order_id=order_id,
                line_no=1,
                sku_id=sku_id,
                product_id=uuid7(),
                name_snapshot="Test product",
                unit_price_minor=1000,
                qty=1,
                line_total_minor=1000,
            )
        )
        # Flush the order first: CheckoutSaga's FK needs it to exist, and
        # neither ORM model declares a ForeignKey() the unit-of-work could
        # use to order this automatically (consistent with every other
        # service's models.py -- the FK lives in the migration only).
        await session.flush()
        session.add(
            CheckoutSaga(
                order_id=order_id,
                state=state,
                reservation_id=uuid7(),
                payment_id=uuid7(),
                attempts=0,
                next_action_at=now,
                deadline_at=now + timedelta(minutes=15),
                last_error=None,
                version=1,
                updated_at=now,
            )
        )
    return order_id


async def test_inventory_committed_advances_to_capturing_and_requests_capture(
    session_factory: async_sessionmaker,
) -> None:
    order_id = await _seed(session_factory, state="COMMITTING")

    await service.on_inventory_committed(session_factory, order_id=order_id)

    async with session_factory() as session:
        saga = await session.get(CheckoutSaga, order_id)
    assert saga is not None and saga.state == "CAPTURING"


async def test_inventory_committed_is_idempotent_on_replay(
    session_factory: async_sessionmaker,
) -> None:
    order_id = await _seed(session_factory, state="COMMITTING")
    await service.on_inventory_committed(session_factory, order_id=order_id)

    # A redelivered event finds the saga already past COMMITTING -- no-op.
    await service.on_inventory_committed(session_factory, order_id=order_id)

    async with session_factory() as session:
        saga = await session.get(CheckoutSaga, order_id)
    assert saga is not None and saga.state == "CAPTURING"


async def test_payment_succeeded_confirms_the_order(session_factory: async_sessionmaker) -> None:
    order_id = await _seed(session_factory, state="CAPTURING")

    await service.on_payment_succeeded(session_factory, order_id=order_id, payment_id=uuid7())

    async with session_factory() as session:
        order = await session.get(Order, order_id)
        saga = await session.get(CheckoutSaga, order_id)
    assert order is not None and order.status == "confirmed"
    assert order.payment_status == "captured"
    assert saga is not None and saga.state == "COMPLETED"


async def test_payment_failed_while_auth_pending_releases_and_cancels(
    session_factory: async_sessionmaker,
) -> None:

    order_id = await _seed(session_factory, state="AUTH_PENDING", payment_status="authorizing")
    inventory_client = FakeInventoryClient()
    async with session_factory() as session:
        order = await session.get(Order, order_id)
    assert order is not None
    inventory_client.reservations[order_id] = {"id": uuid7(), "lines": [], "org_id": order.org_id}

    await service.on_payment_failed(
        session_factory, inventory_client, order_id=order_id, org_id=order.org_id
    )

    async with session_factory() as session:
        saga = await session.get(CheckoutSaga, order_id)
        refreshed = await session.get(Order, order_id)
    assert saga is not None and saga.state == "CANCELLED"
    assert refreshed is not None and refreshed.status == "cancelled"
    assert refreshed.cancel_reason == "payment_declined"
    assert inventory_client.released[order_id] == "payment_declined"


async def test_payment_failed_while_capturing_is_treated_as_capture_failed(
    session_factory: async_sessionmaker,
) -> None:

    order_id = await _seed(session_factory, state="CAPTURING")
    inventory_client = FakeInventoryClient()
    async with session_factory() as session:
        order = await session.get(Order, order_id)
    assert order is not None

    await service.on_payment_failed(
        session_factory, inventory_client, order_id=order_id, org_id=order.org_id
    )

    async with session_factory() as session:
        saga = await session.get(CheckoutSaga, order_id)
        refreshed = await session.get(Order, order_id)
    assert saga is not None and saga.state == "CANCELLED"
    assert refreshed is not None and refreshed.status == "cancelled"
    assert refreshed.cancel_reason == "capture_failed"


async def test_inventory_reservation_expired_while_committing_cancels(
    session_factory: async_sessionmaker,
) -> None:
    order_id = await _seed(session_factory, state="COMMITTING")

    await service.on_inventory_reservation_expired(session_factory, order_id=order_id)

    async with session_factory() as session:
        saga = await session.get(CheckoutSaga, order_id)
        order = await session.get(Order, order_id)
    assert saga is not None and saga.state == "CANCELLED"
    assert order is not None and order.cancel_reason == "reservation_expired"


async def test_payment_authorized_resumes_to_committing(
    session_factory: async_sessionmaker,
) -> None:
    order_id = await _seed(session_factory, state="AUTH_PENDING", payment_status="authorizing")

    await service.on_payment_authorized(session_factory, order_id=order_id, payment_id=uuid7())

    async with session_factory() as session:
        saga = await session.get(CheckoutSaga, order_id)
        order = await session.get(Order, order_id)
    assert saga is not None and saga.state == "COMMITTING"
    assert order is not None and order.payment_status == "authorized"
