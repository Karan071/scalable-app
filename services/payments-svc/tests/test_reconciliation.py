"""Integration tests against the local Compose Postgres (`make test-env`).
Skip if unreachable. R2 (LLD-06 §9): unknown-payment reconciliation.
"""

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4, uuid7

import pytest
import pytest_asyncio
from app import service
from app.models import PaymentAttempt
from fakes import FakeProviderClient
from sf_db import build_session_factory, transaction
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


async def test_reconcile_resolves_a_stale_unknown_attempt(
    session_factory: async_sessionmaker,
) -> None:
    fake = FakeProviderClient()
    fake.force_authorize_timeout("pm_mock_timeout")
    org_id, order_id, customer_id = uuid4(), uuid7(), uuid7()
    outcome = await service.authorize(
        session_factory,
        fake,
        order_id=order_id,
        org_id=org_id,
        customer_id=customer_id,
        amount_minor=1500,
        currency="EUR",
        payment_method_token="pm_mock_timeout",
    )
    assert outcome.status == "unknown"
    # Back-date it past the reconciliation window, and let the provider
    # succeed on the retry (as if it had resolved in the meantime).
    async with transaction(session_factory) as session:
        attempt = await session.get(PaymentAttempt, outcome.payment_id)
        assert attempt is not None
        attempt.updated_at = datetime.now(UTC) - timedelta(seconds=60)
    fake.timeout_tokens.clear()

    resolved = await service.reconcile_unknown_attempts(session_factory, fake, older_than_s=30)

    # >= 1, not == 1: this persistent test DB can carry other stale
    # "unknown" attempts left over from earlier test runs (same reasoning
    # as every other hardcoded-ID fix in this suite) -- what this test
    # actually owns is whether *its* attempt got resolved.
    assert resolved >= 1
    async with session_factory() as session:
        refreshed = await session.get(PaymentAttempt, outcome.payment_id)
    assert refreshed is not None and refreshed.status == "authorized"


async def test_reconcile_leaves_recent_unknown_attempts_alone(
    session_factory: async_sessionmaker,
) -> None:
    fake = FakeProviderClient()
    fake.force_authorize_timeout("pm_mock_timeout")
    org_id, order_id, customer_id = uuid4(), uuid7(), uuid7()
    outcome = await service.authorize(
        session_factory,
        fake,
        order_id=order_id,
        org_id=org_id,
        customer_id=customer_id,
        amount_minor=1500,
        currency="EUR",
        payment_method_token="pm_mock_timeout",
    )
    assert outcome.status == "unknown"

    resolved = await service.reconcile_unknown_attempts(session_factory, fake, older_than_s=30)

    assert resolved == 0
    async with session_factory() as session:
        refreshed = await session.get(PaymentAttempt, outcome.payment_id)
    assert refreshed is not None and refreshed.status == "unknown"
