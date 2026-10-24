"""Integration test against the local Compose Postgres (`make test-env`). Skip
if unreachable. Covers the nightly abandon-expired-carts job (LLD-06 §2).
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from app import repository, service
from sf_db import build_session_factory
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def engine() -> AsyncIterator[AsyncEngine]:
    import os

    try:
        eng = create_async_engine(
            f"postgresql+asyncpg://svc_cart:{os.environ['SF_DB_PASSWORD']}"
            f"@{os.environ['SF_DB_HOST']}:{os.environ['SF_DB_PORT']}/{os.environ['SF_DB_NAME']}"
        )
        async with eng.connect():
            pass
    except Exception as exc:
        pytest.skip(f"local Postgres not reachable: {exc}")
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture(loop_scope="module")
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    factory = build_session_factory(engine)
    async with factory() as s:
        yield s
        await s.commit()


async def _make_cart(session: AsyncSession, *, expires_at: datetime) -> UUID:
    customer_id, org_id = uuid4(), uuid4()
    cart = await repository.get_or_create_active_cart(
        session, customer_id=customer_id, org_id=org_id
    )
    cart.expires_at = expires_at
    return cart.id


async def test_abandon_expired_carts_marks_only_the_expired_active_ones(
    session: AsyncSession,
) -> None:
    now = datetime.now(UTC)
    expired = await _make_cart(session, expires_at=now - timedelta(days=1))
    fresh = await _make_cart(session, expires_at=now + timedelta(days=29))

    count = await service.abandon_expired_carts(session)

    assert count >= 1  # at least this test's own expired cart
    from app.models import Cart

    assert (await session.get(Cart, expired)).status == "abandoned"
    assert (await session.get(Cart, fresh)).status == "active"
