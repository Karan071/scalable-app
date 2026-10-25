"""Integration tests against the local Compose Postgres (`make test-env`).
Skip if unreachable. LLD-06 §5.3's recovery worker: deadline compensation,
and the COMMITTING/CAPTURING defensive re-emit. See ``service.recover_one``'s
docstring for why RESERVING/AUTHORIZING/AUTH_PENDING *within* the deadline
only re-lease rather than retry (no token to retry with).
"""

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid7

import pytest
import pytest_asyncio
from app import repository, service
from app.models import CheckoutSaga, Order, Outbox
from fakes import FakeCartClient, FakeCatalogClient, FakeInventoryClient, FakePaymentsClient
from sf_db import build_session_factory, transaction
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from test_saga_continuation import _seed

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


async def _make_past_deadline(session_factory: async_sessionmaker, order_id) -> None:
    async with transaction(session_factory) as session:
        saga = await session.get(CheckoutSaga, order_id)
        assert saga is not None
        saga.deadline_at = datetime.now(UTC) - timedelta(seconds=1)


async def test_past_deadline_authorizing_releases_and_cancels(
    session_factory: async_sessionmaker, participants: service.Participants
) -> None:
    order_id = await _seed(session_factory, state="AUTHORIZING", payment_status="authorizing")
    await _make_past_deadline(session_factory, order_id)
    async with session_factory() as session:
        order = await session.get(Order, order_id)
    assert order is not None
    participants.inventory.reservations[order_id] = {
        "id": uuid7(),
        "lines": [],
        "org_id": order.org_id,
    }

    await service.recover_one(session_factory, participants, order_id=order_id)

    async with session_factory() as session:
        saga = await session.get(CheckoutSaga, order_id)
        refreshed = await session.get(Order, order_id)
    assert saga is not None and saga.state == "CANCELLED"
    assert refreshed is not None and refreshed.cancel_reason == "payment_timeout"
    assert participants.inventory.released[order_id] == "payment_timeout"


async def test_past_deadline_reserving_cancels_without_a_release_call(
    session_factory: async_sessionmaker, participants: service.Participants
) -> None:
    order_id = await _seed(session_factory, state="RESERVING", payment_status="none")
    await _make_past_deadline(session_factory, order_id)

    await service.recover_one(session_factory, participants, order_id=order_id)

    async with session_factory() as session:
        saga = await session.get(CheckoutSaga, order_id)
        order = await session.get(Order, order_id)
    assert saga is not None and saga.state == "CANCELLED"
    assert order is not None and order.cancel_reason == "payment_timeout"
    assert participants.inventory.released == {}  # nothing was ever reserved


async def test_committing_within_budget_re_emits_commit_command(
    session_factory: async_sessionmaker, participants: service.Participants
) -> None:
    order_id = await _seed(session_factory, state="COMMITTING")

    await service.recover_one(session_factory, participants, order_id=order_id)

    async with session_factory() as session:
        saga = await session.get(CheckoutSaga, order_id)
        rows = (
            (await session.execute(select(Outbox).where(Outbox.aggregate_id == str(order_id))))
            .scalars()
            .all()
        )
    assert saga is not None
    assert saga.state == "COMMITTING"  # unchanged -- only re-emits, doesn't advance
    assert any(row.routing_key == "inventory.commit_requested.v1" for row in rows)


async def test_within_deadline_not_yet_due_states_are_left_alone(
    session_factory: async_sessionmaker, participants: service.Participants
) -> None:
    order_id = await _seed(session_factory, state="AUTH_PENDING", payment_status="authorizing")

    await service.recover_one(session_factory, participants, order_id=order_id)

    async with session_factory() as session:
        saga = await session.get(CheckoutSaga, order_id)
        order = await session.get(Order, order_id)
    assert saga is not None and saga.state == "AUTH_PENDING"
    assert order is not None and order.status == "pending"
    assert participants.inventory.released == {}


async def test_terminal_sagas_are_never_claimed(session_factory: async_sessionmaker) -> None:
    order_id = await _seed(
        session_factory, state="COMPLETED", status="confirmed", payment_status="captured"
    )
    async with transaction(session_factory) as session:
        saga = await session.get(CheckoutSaga, order_id)
        assert saga is not None
        saga.next_action_at = datetime.now(UTC) - timedelta(seconds=1)

    async with transaction(session_factory) as session:
        claimed = await repository.claim_due_sagas(session, limit=100)

    assert order_id not in claimed
