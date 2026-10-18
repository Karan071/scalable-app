"""Integration tests against the local Compose Postgres + RabbitMQ (LLD-08 §5.3).

Skip if either is unreachable. Unlike `test_outbox.py`'s `CREATE TEMP
TABLE` trick, the relay opens its own pooled connections independently of
the test's session, so a connection-local temp table wouldn't be visible to
it — a real (but disposable, dropped in teardown) table is used instead.
"""

import asyncio
import json
import os
import time
from collections.abc import AsyncIterator
from uuid import uuid4

import aio_pika
import pytest
import pytest_asyncio
from sf_db import build_engine, build_session_factory
from sf_messaging import OutboxRelay, build_envelope
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_DB_PASSWORD = os.environ.get("SF_TEST_DB_PASSWORD", "app_local_dev")
_AMQP_URL = os.environ.get(
    "SF_TEST_AMQP_URL", "amqp://scaleforge:rabbitmq_local_dev@localhost:5672/"
)
_DSN_ASYNCPG = f"postgresql://svc_users:{_DB_PASSWORD}@localhost:5432/scaleforge"

_DDL = """
CREATE TABLE IF NOT EXISTS test_relay_outbox (
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
)
"""


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = build_engine(
        host="localhost", port=5432, database="scaleforge", user="svc_users", password=_DB_PASSWORD
    )
    try:
        factory = build_session_factory(engine)
        async with factory() as s, s.begin():
            await s.execute(text(_DDL))
    except Exception as exc:
        await engine.dispose()
        pytest.skip(f"local Postgres not reachable: {exc}")
        return
    try:
        yield factory
    finally:
        async with factory() as s, s.begin():
            await s.execute(text("DROP TABLE IF EXISTS test_relay_outbox"))
        await engine.dispose()


@pytest_asyncio.fixture
async def test_queue(session_factory: async_sessionmaker[AsyncSession]):
    try:
        connection = await aio_pika.connect_robust(_AMQP_URL, timeout=3)
    except Exception as exc:
        pytest.skip(f"local RabbitMQ not reachable: {exc}")
        return
    channel = await connection.channel()
    exchange = await channel.declare_exchange("sf.events", type="topic", durable=True, passive=True)
    queue_name = f"q.test.relay-{uuid4().hex[:12]}"
    queue = await channel.declare_queue(queue_name, durable=True)
    await queue.bind(exchange, routing_key="widget.created.v1")
    try:
        yield queue
    finally:
        await queue.delete()
        await connection.close()


async def _consume_one(queue, timeout_s: float) -> bytes | None:
    try:
        async with queue.iterator(timeout=timeout_s) as it:
            async for message in it:
                body = message.body
                await message.ack()
                return body
    except TimeoutError:
        pass
    return None


async def test_relay_publishes_a_pending_row_and_marks_it_published(
    session_factory: async_sessionmaker[AsyncSession], test_queue
) -> None:
    aggregate_id = f"widget-{uuid4().hex[:8]}"
    envelope = build_envelope(
        event_type="widget.created",
        source="test",
        subject=aggregate_id,
        data={"id": aggregate_id},
        schema_version="1.0",
        aggregate_type="widget",
        aggregate_version=1,
    )
    # `add_to_outbox` always targets a table literally named `outbox`; this
    # suite uses its own disposable table, so insert directly instead.
    async with session_factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO test_relay_outbox "
                "(id, exchange, routing_key, aggregate_type, aggregate_id, aggregate_version, "
                "payload, occurred_at) "
                "VALUES (:id, 'sf.events', 'widget.created.v1', 'widget', :aid, 1, :payload, now())"
            ),
            {"id": envelope["id"], "aid": aggregate_id, "payload": json.dumps(envelope)},
        )

    relay = OutboxRelay(
        service_name="test",
        session_factory=session_factory,
        dsn_asyncpg=_DSN_ASYNCPG,
        amqp_url=_AMQP_URL,
        notify_channel="outbox_relay_test",
        outbox_table="test_relay_outbox",
        poll_interval_s=0.1,
    )
    await relay.start()
    try:
        body = await _consume_one(test_queue, timeout_s=5)
        assert body is not None
        assert json.loads(body)["id"] == envelope["id"]

        await asyncio.sleep(0.5)  # let the relay's own mark-published commit land
        async with session_factory() as session:
            row = (
                await session.execute(
                    text("SELECT published_at FROM test_relay_outbox WHERE id = :id"),
                    {"id": envelope["id"]},
                )
            ).first()
        assert row is not None
        assert row[0] is not None
    finally:
        await relay.stop()


async def test_relay_reacts_to_notify_faster_than_a_long_poll_interval(
    session_factory: async_sessionmaker[AsyncSession], test_queue
) -> None:
    relay = OutboxRelay(
        service_name="test",
        session_factory=session_factory,
        dsn_asyncpg=_DSN_ASYNCPG,
        amqp_url=_AMQP_URL,
        notify_channel="outbox_relay_test2",
        outbox_table="test_relay_outbox",
        poll_interval_s=10.0,  # deliberately long: a fast pickup can only be LISTEN/NOTIFY
    )
    await relay.start()
    await asyncio.sleep(0.3)  # let the LISTEN connection actually establish

    aggregate_id = f"widget-{uuid4().hex[:8]}"
    envelope = build_envelope(
        event_type="widget.created",
        source="test",
        subject=aggregate_id,
        data={"id": aggregate_id},
        schema_version="1.0",
        aggregate_type="widget",
        aggregate_version=1,
    )
    try:
        started = time.monotonic()
        async with session_factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO test_relay_outbox "
                    "(id, exchange, routing_key, aggregate_type, aggregate_id, aggregate_version, "
                    "payload, occurred_at) "
                    "VALUES (:id, 'sf.events', 'widget.created.v1', 'widget', "
                    ":aid, 1, :payload, now())"
                ),
                {"id": envelope["id"], "aid": aggregate_id, "payload": json.dumps(envelope)},
            )
            await session.execute(text("NOTIFY outbox_relay_test2"))

        body = await _consume_one(test_queue, timeout_s=5)
        elapsed = time.monotonic() - started
        assert body is not None
        assert elapsed < 10.0, "pickup took as long as the poll interval -- NOTIFY path didn't fire"
    finally:
        await relay.stop()
