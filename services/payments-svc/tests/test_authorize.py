"""Integration tests against the local Compose Postgres (`make test-env`).
Skip if unreachable. LLD-06 §7.1-§7.2: authorize, its idempotency, and
the "unknown" (provider timeout) path -- at the ``service`` layer
directly, against ``FakeProviderClient`` (mock-payment-provider doesn't
run during these tests).
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


async def test_authorize_success(
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

    assert outcome.status_code == 200
    assert outcome.status == "authorized"
    async with session_factory() as session:
        attempt = await session.get(PaymentAttempt, outcome.payment_id)
    assert attempt is not None
    assert attempt.provider_intent_id is not None
    assert attempt.authorized_at is not None


async def test_authorize_decline_is_402(
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
        payment_method_token="pm_mock_decline",
    )

    assert outcome.status_code == 402
    assert outcome.status == "declined"


async def test_authorize_timeout_is_202_unknown(
    session_factory: async_sessionmaker, provider: FakeProviderClient
) -> None:
    provider.force_authorize_timeout("pm_mock_timeout")
    org_id, order_id, customer_id = uuid4(), uuid7(), uuid7()

    outcome = await service.authorize(
        session_factory,
        provider,
        order_id=order_id,
        org_id=org_id,
        customer_id=customer_id,
        amount_minor=1500,
        currency="EUR",
        payment_method_token="pm_mock_timeout",
    )

    assert outcome.status_code == 202
    assert outcome.status == "unknown"


async def test_authorize_replay_same_order_returns_same_outcome(
    session_factory: async_sessionmaker, provider: FakeProviderClient
) -> None:
    org_id, order_id, customer_id = uuid4(), uuid7(), uuid7()

    first = await service.authorize(
        session_factory,
        provider,
        order_id=order_id,
        org_id=org_id,
        customer_id=customer_id,
        amount_minor=1500,
        currency="EUR",
        payment_method_token="pm_mock_success",
    )
    second = await service.authorize(
        session_factory,
        provider,
        order_id=order_id,
        org_id=org_id,
        customer_id=customer_id,
        amount_minor=1500,
        currency="EUR",
        payment_method_token="pm_mock_success",
    )

    assert first.payment_id == second.payment_id
    assert second.status == "authorized"
    assert len(provider.intents) == 1  # the provider only ever saw one real authorize


async def test_authorize_resumes_an_unknown_attempt_to_authorized(
    session_factory: async_sessionmaker, provider: FakeProviderClient
) -> None:
    provider.force_authorize_timeout("pm_mock_timeout")
    org_id, order_id, customer_id = uuid4(), uuid7(), uuid7()
    first = await service.authorize(
        session_factory,
        provider,
        order_id=order_id,
        org_id=org_id,
        customer_id=customer_id,
        amount_minor=1500,
        currency="EUR",
        payment_method_token="pm_mock_timeout",
    )
    assert first.status == "unknown"

    # The client retries with the *same* request (same order_id, same
    # token) -- the provider no longer times out this time around.
    provider.timeout_tokens.clear()
    second = await service.authorize(
        session_factory,
        provider,
        order_id=order_id,
        org_id=org_id,
        customer_id=customer_id,
        amount_minor=1500,
        currency="EUR",
        payment_method_token="pm_mock_timeout",
    )

    assert second.status == "authorized"
    assert second.payment_id == first.payment_id
