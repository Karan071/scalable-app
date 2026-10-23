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


async def _insert_row(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    exchange: str,
    routing_key: str,
    occurred_at_sql: str = "now()",
) -> str:
    aggregate_id = f"widget-{uuid4().hex[:8]}"
    envelope = build_envelope(
        event_type=routing_key.removesuffix(".v1"),
        source="test",
        subject=aggregate_id,
        data={"id": aggregate_id},
        schema_version="1.0",
        aggregate_type="widget",
        aggregate_version=1,
    )
    async with session_factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO test_relay_outbox "
                "(id, exchange, routing_key, aggregate_type, aggregate_id, aggregate_version, "
                f"payload, occurred_at) VALUES (:id, :ex, :rk, 'widget', :aid, 1, :payload, "
                f"{occurred_at_sql})"
            ),
            {
                "id": envelope["id"],
                "ex": exchange,
                "rk": routing_key,
                "aid": aggregate_id,
                "payload": json.dumps(envelope),
            },
        )
    return envelope["id"]


async def _row_state(
    session_factory: async_sessionmaker[AsyncSession], row_id: str
) -> tuple[object, int, str | None]:
    async with session_factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT published_at, attempts, last_error FROM test_relay_outbox "
                    "WHERE id = :id"
                ),
                {"id": row_id},
            )
        ).one()
    return row[0], row[1], row[2]


def _relay(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    service_name: str = "test",
    **kwargs: object,
) -> OutboxRelay:
    return OutboxRelay(
        service_name=service_name,
        session_factory=session_factory,
        dsn_asyncpg=_DSN_ASYNCPG,
        amqp_url=_AMQP_URL,
        notify_channel="outbox_relay_test3",
        outbox_table="test_relay_outbox",
        poll_interval_s=0.05,
        **kwargs,  # type: ignore[arg-type]
    )


async def test_an_event_with_no_bound_queue_is_marked_published_not_retried(
    session_factory: async_sessionmaker[AsyncSession], test_queue, read_metric
) -> None:
    row_id = await _insert_row(
        session_factory, exchange="sf.events", routing_key=f"nobody.{uuid4().hex}.v1"
    )
    service = f"svc-{uuid4().hex[:8]}"
    relay = _relay(session_factory, service_name=service)
    await relay.start()
    try:
        await asyncio.sleep(0.6)
    finally:
        await relay.stop()

    published_at, attempts, last_error = await _row_state(session_factory, row_id)
    assert published_at is not None
    assert attempts == 0
    assert last_error is None
    assert read_metric("outbox_publish_total", service=service, result="no_subscriber") == 1


async def test_a_command_with_no_bound_queue_stays_unpublished_and_is_retried(
    session_factory: async_sessionmaker[AsyncSession], test_queue, read_metric
) -> None:
    row_id = await _insert_row(
        session_factory, exchange="sf.commands", routing_key=f"nobody.{uuid4().hex}.v1"
    )
    service = f"svc-{uuid4().hex[:8]}"
    relay = _relay(session_factory, service_name=service)
    await relay.start()
    try:
        await asyncio.sleep(0.6)
    finally:
        await relay.stop()

    published_at, attempts, last_error = await _row_state(session_factory, row_id)
    assert published_at is None
    assert attempts >= 1
    assert last_error is not None and "NO_ROUTE" in last_error
    assert read_metric("outbox_publish_total", service=service, result="returned") >= 1
    assert read_metric("outbox_publish_total", service=service, result="confirmed") == 0


async def test_rows_that_keep_failing_cannot_starve_newer_ones(
    session_factory: async_sessionmaker[AsyncSession], test_queue
) -> None:
    """More permanently failing rows than one batch holds, all older than
    a routable row: ordering by ``occurred_at`` alone re-claims the same
    failures every tick and never reaches the routable row."""
    for _ in range(5):
        await _insert_row(
            session_factory,
            exchange="sf.commands",
            routing_key=f"nobody.{uuid4().hex}.v1",
            occurred_at_sql="now() - interval '1 hour'",
        )
    fresh_id = await _insert_row(
        session_factory, exchange="sf.events", routing_key="widget.created.v1"
    )

    relay = _relay(session_factory, batch_size=3)
    await relay.start()
    try:
        body = await _consume_one(test_queue, timeout_s=5)
    finally:
        await relay.stop()

    assert body is not None
    assert json.loads(body)["id"] == fresh_id


async def test_relay_reports_publish_latency_and_drains_the_backlog_gauges(
    session_factory: async_sessionmaker[AsyncSession], test_queue, read_metric
) -> None:
    await _insert_row(
        session_factory,
        exchange="sf.events",
        routing_key="widget.created.v1",
        occurred_at_sql="now() - interval '2 seconds'",
    )
    service = f"svc-{uuid4().hex[:8]}"
    relay = _relay(session_factory, service_name=service, backlog_refresh_s=0.0)
    await relay.start()
    try:
        assert await _consume_one(test_queue, timeout_s=5) is not None
        await asyncio.sleep(0.4)  # a few more ticks: the backlog refresh after the drain
    finally:
        await relay.stop()

    assert read_metric("outbox_publish_total", service=service, result="confirmed") == 1
    assert read_metric("outbox_publish_latency_seconds", service=service) == 1
    assert read_metric("outbox_pending", service=service) == 0
    assert read_metric("outbox_oldest_unpublished_age_seconds", service=service) == 0
