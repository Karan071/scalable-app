"""Integration test against the local Compose Postgres (LLD-08 §5).

Skips if that stack isn't up — `make dev`, then run with network access to
localhost:5432 as `svc_users` (deploy/compose/postgres/init-schemas.sh).
"""

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sf_db import build_engine, build_session_factory
from sf_messaging import add_to_outbox, build_envelope
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_DDL = """
CREATE TEMP TABLE outbox (
  id uuid PRIMARY KEY,
  exchange text NOT NULL,
  routing_key text NOT NULL,
  aggregate_type text NOT NULL,
  aggregate_id text NOT NULL,
  aggregate_version int NOT NULL,
  payload jsonb NOT NULL,
  occurred_at timestamptz NOT NULL,
  published_at timestamptz,
  attempts int NOT NULL DEFAULT 0,
  last_error text
) ON COMMIT PRESERVE ROWS
"""


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = build_engine(
        host="localhost",
        port=5432,
        database="scaleforge",
        user="svc_users",
        password="app_local_dev",
    )
    try:
        factory = build_session_factory(engine)
        async with factory() as s:
            await s.execute(text(_DDL))
            yield s
    except Exception as exc:
        pytest.skip(f"local Postgres not reachable: {exc}")
    finally:
        await engine.dispose()


async def test_add_inserts_a_pending_row(session: AsyncSession) -> None:
    envelope = build_envelope(
        event_type="user.created",
        source="users-svc",
        subject="user-abc",
        data={"user_id": "user-abc", "status": "active"},
        schema_version="1.0",
        aggregate_type="user",
        aggregate_version=1,
    )

    await add_to_outbox(
        session,
        envelope,
        exchange="sf.events",
        routing_key="user.created.v1",
        aggregate_id="user-abc",
        notify_channel="outbox_users",
    )

    row = (
        (
            await session.execute(
                text(
                    "SELECT exchange, routing_key, payload, published_at FROM outbox WHERE id = :id"
                ),
                {"id": envelope["id"]},
            )
        )
        .mappings()
        .one()
    )

    assert row["exchange"] == "sf.events"
    assert row["routing_key"] == "user.created.v1"
    assert row["published_at"] is None
    assert row["payload"]["type"] == "user.created"


async def test_rejects_an_unsafe_notify_channel(session: AsyncSession) -> None:
    envelope = build_envelope(
        event_type="user.created",
        source="users-svc",
        subject="user-abc",
        data={},
        schema_version="1.0",
        aggregate_type="user",
        aggregate_version=1,
    )

    with pytest.raises(ValueError, match="NOTIFY channel"):
        await add_to_outbox(
            session,
            envelope,
            exchange="sf.events",
            routing_key="user.created.v1",
            aggregate_id="user-abc",
            notify_channel="outbox_users; DROP TABLE outbox",
        )
