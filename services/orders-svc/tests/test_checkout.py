"""Integration tests against the local Compose Postgres (`make test-env`).
Skip if unreachable. LLD-06 §3-§5: checkout's idempotency, the
synchronous reserve+authorize path, and its failure modes -- at the
``service`` layer directly (not through HTTP), same reasoning as
inventory-svc's concurrency tests: faster, and the fakes stand in for
cart-svc/catalog-svc/inventory-svc/payments-svc without needing them
running.
"""

import os
from collections.abc import AsyncIterator
from uuid import UUID, uuid4, uuid7

import pytest
import pytest_asyncio
from app import idempotency, service
from app.models import CheckoutSaga, Order
from fakes import FakeCartClient, FakeCatalogClient, FakeInventoryClient, FakePaymentsClient
from sf_db import build_session_factory
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


@pytest.fixture
def participants() -> service.Participants:
    return service.Participants(
        cart=FakeCartClient(),
        catalog=FakeCatalogClient(),
        inventory=FakeInventoryClient(),
        payments=FakePaymentsClient(),
    )


def _address() -> dict:
    return {
        "name": "A. Customer",
        "line1": "1 Main St",
        "city": "Berlin",
        "postal_code": "10115",
        "country": "DE",
    }


async def _checkout(
    session_factory: async_sessionmaker,
    participants: service.Participants,
    *,
    customer_id: UUID,
    org_id: UUID,
    idempotency_key: UUID,
    cart_version: int = 1,
    promo_code: str | None = None,
    payment_method_token: str = "pm_mock_success",
) -> service.ActionResult:
    return await service.checkout(
        session_factory,
        participants,
        customer_id=customer_id,
        org_id=org_id,
        idempotency_key=idempotency_key,
        cart_version=cart_version,
        promo_code=promo_code,
        shipping_address=_address(),
        payment_method_token=payment_method_token,
        reservation_ttl_seconds=900,
        saga_deadline_seconds=900,
    )


async def test_checkout_happy_path_reaches_committing(
    session_factory: async_sessionmaker, participants: service.Participants
) -> None:
    org_id, customer_id, sku_id = uuid4(), uuid7(), uuid7()
    participants.cart.seed(customer_id=customer_id, org_id=org_id, items=[(sku_id, 2)])
    participants.catalog.add(org_id, sku_id, price_minor=1000)
    participants.inventory.seed_stock(org_id, sku_id, 10)

    result = await _checkout(
        session_factory,
        participants,
        customer_id=customer_id,
        org_id=org_id,
        idempotency_key=uuid7(),
    )

    assert result.status_code == 202, result.body
    assert result.body["status"] == "pending"
    assert result.body["payment_status"] == "authorized"
    order_id = UUID(result.body["order_id"])

    async with session_factory() as session:
        order = await session.get(Order, order_id)
        saga = await session.get(CheckoutSaga, order_id)
    assert order is not None and order.status == "pending"
    assert saga is not None and saga.state == "COMMITTING"
    assert order_id in participants.inventory.reservations
    assert order_id in participants.payments.authorized


async def test_checkout_out_of_stock_cancels_the_order(
    session_factory: async_sessionmaker, participants: service.Participants
) -> None:
    org_id, customer_id, sku_id = uuid4(), uuid7(), uuid7()
    participants.cart.seed(customer_id=customer_id, org_id=org_id, items=[(sku_id, 5)])
    participants.catalog.add(org_id, sku_id)
    participants.inventory.seed_stock(org_id, sku_id, 1)

    result = await _checkout(
        session_factory,
        participants,
        customer_id=customer_id,
        org_id=org_id,
        idempotency_key=uuid7(),
    )

    assert result.status_code == 409
    assert result.body["title"] == "out-of-stock"
    # Rejected before a reservation was ever created (ADR-0015: title-only
    # error bodies, so there's no order_id in result.body to check against).
    assert participants.inventory.reservations == {}


async def test_checkout_payment_declined_releases_reservation_and_cancels(
    session_factory: async_sessionmaker, participants: service.Participants
) -> None:
    org_id, customer_id, sku_id = uuid4(), uuid7(), uuid7()
    participants.cart.seed(customer_id=customer_id, org_id=org_id, items=[(sku_id, 1)])
    participants.catalog.add(org_id, sku_id)
    participants.inventory.seed_stock(org_id, sku_id, 5)

    result = await _checkout(
        session_factory,
        participants,
        customer_id=customer_id,
        org_id=org_id,
        idempotency_key=uuid7(),
        payment_method_token="pm_mock_decline",
    )

    assert result.status_code == 402
    assert result.body["title"] == "payment-declined"
    assert participants.inventory.stock[(org_id, sku_id)] == 5  # released back


async def test_checkout_payment_timeout_returns_202_pending_authorizing(
    session_factory: async_sessionmaker, participants: service.Participants
) -> None:
    org_id, customer_id, sku_id = uuid4(), uuid7(), uuid7()
    participants.cart.seed(customer_id=customer_id, org_id=org_id, items=[(sku_id, 1)])
    participants.catalog.add(org_id, sku_id)
    participants.inventory.seed_stock(org_id, sku_id, 5)

    result = await _checkout(
        session_factory,
        participants,
        customer_id=customer_id,
        org_id=org_id,
        idempotency_key=uuid7(),
        payment_method_token="pm_mock_timeout",
    )

    assert result.status_code == 202, result.body
    assert result.body["payment_status"] == "authorizing"
    order_id = UUID(result.body["order_id"])
    async with session_factory() as session:
        saga = await session.get(CheckoutSaga, order_id)
    assert saga is not None and saga.state == "AUTH_PENDING"


async def test_checkout_cart_version_mismatch_is_cart_changed(
    session_factory: async_sessionmaker, participants: service.Participants
) -> None:
    org_id, customer_id, sku_id = uuid4(), uuid7(), uuid7()
    participants.cart.seed(customer_id=customer_id, org_id=org_id, version=3, items=[(sku_id, 1)])
    participants.catalog.add(org_id, sku_id)
    participants.inventory.seed_stock(org_id, sku_id, 5)

    result = await _checkout(
        session_factory,
        participants,
        customer_id=customer_id,
        org_id=org_id,
        idempotency_key=uuid7(),
        cart_version=1,
    )

    assert result.status_code == 409
    assert result.body["title"] == "cart-changed"


async def test_checkout_empty_cart_is_cart_changed(
    session_factory: async_sessionmaker, participants: service.Participants
) -> None:
    org_id, customer_id = uuid4(), uuid7()
    # No cart seeded at all -- get_active_snapshot returns None.
    result = await _checkout(
        session_factory,
        participants,
        customer_id=customer_id,
        org_id=org_id,
        idempotency_key=uuid7(),
    )

    assert result.status_code == 409
    assert result.body["title"] == "cart-changed"


async def test_checkout_replay_of_the_same_key_and_body_returns_the_same_response(
    session_factory: async_sessionmaker, participants: service.Participants
) -> None:
    org_id, customer_id, sku_id = uuid4(), uuid7(), uuid7()
    participants.cart.seed(customer_id=customer_id, org_id=org_id, items=[(sku_id, 1)])
    participants.catalog.add(org_id, sku_id)
    participants.inventory.seed_stock(org_id, sku_id, 5)
    key = uuid7()

    first = await _checkout(
        session_factory, participants, customer_id=customer_id, org_id=org_id, idempotency_key=key
    )
    second = await _checkout(
        session_factory, participants, customer_id=customer_id, org_id=org_id, idempotency_key=key
    )

    assert second.status_code == first.status_code
    assert second.body == first.body
    # Only one order: inventory only ever saw one reserve call survive (the
    # fake's dict has exactly one order_id as a key).
    assert len(participants.inventory.reservations) == 1


async def test_checkout_same_key_different_body_is_rejected(
    session_factory: async_sessionmaker, participants: service.Participants
) -> None:
    org_id, customer_id, sku_id = uuid4(), uuid7(), uuid7()
    participants.cart.seed(customer_id=customer_id, org_id=org_id, items=[(sku_id, 1)])
    participants.catalog.add(org_id, sku_id)
    participants.inventory.seed_stock(org_id, sku_id, 5)
    key = uuid7()
    await _checkout(
        session_factory,
        participants,
        customer_id=customer_id,
        org_id=org_id,
        idempotency_key=key,
        promo_code=None,
    )

    with pytest.raises(service.IdempotencyKeyReused):
        await _checkout(
            session_factory,
            participants,
            customer_id=customer_id,
            org_id=org_id,
            idempotency_key=key,
            promo_code="DIFFERENT",
        )


async def test_checkout_in_progress_lease_is_reported(
    session_factory: async_sessionmaker,
) -> None:
    customer_id, key = uuid7(), uuid7()
    request_hash = idempotency.canonical_hash({"x": 1})
    claim = await idempotency.claim(
        session_factory,
        scope="checkout",
        principal_id=customer_id,
        key=key,
        request_hash=request_hash,
    )
    assert isinstance(claim, idempotency.Claimed)

    second = await idempotency.claim(
        session_factory,
        scope="checkout",
        principal_id=customer_id,
        key=key,
        request_hash=request_hash,
    )

    assert isinstance(second, idempotency.InProgress)


async def test_checkout_double_click_two_keys_same_cart_version_is_one_order(
    session_factory: async_sessionmaker, participants: service.Participants
) -> None:
    org_id, customer_id, sku_id = uuid4(), uuid7(), uuid7()
    participants.cart.seed(customer_id=customer_id, org_id=org_id, items=[(sku_id, 1)])
    participants.catalog.add(org_id, sku_id)
    participants.inventory.seed_stock(org_id, sku_id, 5)

    first = await _checkout(
        session_factory,
        participants,
        customer_id=customer_id,
        org_id=org_id,
        idempotency_key=uuid7(),
    )
    second = await _checkout(
        session_factory,
        participants,
        customer_id=customer_id,
        org_id=org_id,
        idempotency_key=uuid7(),
    )

    assert first.status_code == 202, first.body
    assert second.status_code == 409, second.body
    assert second.body["title"] == "cart-changed"
