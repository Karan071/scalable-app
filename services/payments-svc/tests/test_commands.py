"""Integration tests against the local Compose Postgres (`make test-env`).
Skip if unreachable. LLD-06 §5.1/§6: the capture/void/refund commands
orders-svc's checkout saga and order admin ops emit.
"""

import os
from collections.abc import AsyncIterator
from uuid import uuid4, uuid7

import pytest
import pytest_asyncio
from app import service
from app.models import PaymentAttempt
from fakes import FakeProviderClient
from sf_db import build_session_factory
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def engine() -> AsyncIterator[AsyncEngine]:
    try:
        eng = create_async_engine(
            f"postgresql+asyncpg://svc_payments:{os.environ['SF_DB_PASSWORD']}"
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
def provider() -> FakeProviderClient:
    return FakeProviderClient()


async def _authorized_order(session_factory: async_sessionmaker, provider: FakeProviderClient):
    org_id, order_id, customer_id = uuid4(), uuid7(), uuid7()
    outcome = await service.authorize(
        session_factory,
        provider,
        order_id=order_id,
        org_id=org_id,
        customer_id=customer_id,
        amount_minor=1500,
        currency="EUR",
        payment_method_token="pm_mock_success",
    )
    assert outcome.status == "authorized"
    return order_id, outcome.payment_id


async def test_capture_requested_marks_captured(
    session_factory: async_sessionmaker, provider: FakeProviderClient
) -> None:
    order_id, payment_id = await _authorized_order(session_factory, provider)

    await service.handle_capture_requested(session_factory, provider, order_id=order_id)

    async with session_factory() as session:
        attempt = await session.get(PaymentAttempt, payment_id)
    assert attempt is not None
    assert attempt.status == "captured"
    assert attempt.captured_at is not None


async def test_capture_requested_is_idempotent_on_replay(
    session_factory: async_sessionmaker, provider: FakeProviderClient
) -> None:
    order_id, payment_id = await _authorized_order(session_factory, provider)
    await service.handle_capture_requested(session_factory, provider, order_id=order_id)

    await service.handle_capture_requested(session_factory, provider, order_id=order_id)

    async with session_factory() as session:
        attempt = await session.get(PaymentAttempt, payment_id)
    assert attempt is not None and attempt.status == "captured"


async def test_capture_failed_reverts_to_authorized_and_emits_failed(
    session_factory: async_sessionmaker, provider: FakeProviderClient
) -> None:
    org_id, order_id, customer_id = uuid4(), uuid7(), uuid7()
    outcome = await service.authorize(
        session_factory,
        provider,
        order_id=order_id,
        org_id=org_id,
        customer_id=customer_id,
        amount_minor=1500,
        currency="EUR",
        payment_method_token="pm_mock_success",
    )
    async with session_factory() as session:
        attempt = await session.get(PaymentAttempt, outcome.payment_id)
    assert attempt is not None and attempt.provider_intent_id is not None
    provider.force_capture_fail(attempt.provider_intent_id)

    await service.handle_capture_requested(session_factory, provider, order_id=order_id)

    async with session_factory() as session:
        refreshed = await session.get(PaymentAttempt, outcome.payment_id)
    assert refreshed is not None
    assert refreshed.status == "authorized"  # LLD-06 §7.2: capture failed -> back to authorized
    assert refreshed.last_error_code == "capture_failed"


async def test_void_requested_marks_voided(
    session_factory: async_sessionmaker, provider: FakeProviderClient
) -> None:
    order_id, payment_id = await _authorized_order(session_factory, provider)

    await service.handle_void_requested(session_factory, provider, order_id=order_id)

    async with session_factory() as session:
        attempt = await session.get(PaymentAttempt, payment_id)
    assert attempt is not None and attempt.status == "voided"


async def test_void_requested_on_a_captured_payment_is_a_no_op(
    session_factory: async_sessionmaker, provider: FakeProviderClient
) -> None:
    order_id, payment_id = await _authorized_order(session_factory, provider)
    await service.handle_capture_requested(session_factory, provider, order_id=order_id)

    await service.handle_void_requested(session_factory, provider, order_id=order_id)

    async with session_factory() as session:
        attempt = await session.get(PaymentAttempt, payment_id)
    assert attempt is not None and attempt.status == "captured"  # unchanged


async def test_refund_requested_on_captured_payment_marks_refunded(
    session_factory: async_sessionmaker, provider: FakeProviderClient
) -> None:
    order_id, payment_id = await _authorized_order(session_factory, provider)
    await service.handle_capture_requested(session_factory, provider, order_id=order_id)

    await service.handle_refund_requested(session_factory, provider, order_id=order_id)

    async with session_factory() as session:
        attempt = await session.get(PaymentAttempt, payment_id)
        refund = await service.repository.get_refund_by_payment(session, payment_id)
    assert attempt is not None and attempt.status == "refunded"
    assert refund is not None and refund.status == "refunded"


async def test_refund_requested_is_idempotent_on_replay(
    session_factory: async_sessionmaker, provider: FakeProviderClient
) -> None:
    order_id, payment_id = await _authorized_order(session_factory, provider)
    await service.handle_capture_requested(session_factory, provider, order_id=order_id)
    await service.handle_refund_requested(session_factory, provider, order_id=order_id)

    await service.handle_refund_requested(session_factory, provider, order_id=order_id)

    async with session_factory() as session:
        attempt = await session.get(PaymentAttempt, payment_id)
    assert attempt is not None and attempt.status == "refunded"


async def test_refund_requested_without_a_capture_is_a_no_op(
    session_factory: async_sessionmaker, provider: FakeProviderClient
) -> None:
    order_id, payment_id = await _authorized_order(session_factory, provider)

    await service.handle_refund_requested(session_factory, provider, order_id=order_id)

    async with session_factory() as session:
        attempt = await session.get(PaymentAttempt, payment_id)
    assert attempt is not None and attempt.status == "authorized"  # unchanged
