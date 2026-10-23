"""Integration tests against the local Compose Postgres + RabbitMQ (LLD-08 §6)."""

import asyncio
import json
import os
from collections.abc import AsyncIterator
from uuid import uuid4

import aio_pika
import pytest
import pytest_asyncio
from sf_db import build_engine, build_session_factory
from sf_messaging import InboxConsumer, build_envelope
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_DB_PASSWORD = os.environ.get("SF_TEST_DB_PASSWORD", "app_local_dev")
_AMQP_URL = os.environ.get(
    "SF_TEST_AMQP_URL", "amqp://scaleforge:rabbitmq_local_dev@localhost:5672/"
)

_DDL = """
CREATE TABLE IF NOT EXISTS test_consumer_inbox (
  consumer text NOT NULL,
  message_id uuid NOT NULL,
  received_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (consumer, message_id)
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
            await s.execute(text("DROP TABLE IF EXISTS test_consumer_inbox"))
        await engine.dispose()


@pytest_asyncio.fixture
async def test_exchange_and_queue():
    try:
        connection = await aio_pika.connect_robust(_AMQP_URL, timeout=3)
    except Exception as exc:
        pytest.skip(f"local RabbitMQ not reachable: {exc}")
        return
    channel = await connection.channel()
    exchange = await channel.declare_exchange("sf.events", type="topic", durable=True, passive=True)
    queue_name = f"q.test.consumer-{uuid4().hex[:12]}"
    queue = await channel.declare_queue(queue_name, durable=True)
    await queue.bind(exchange, routing_key="gadget.created.v1")
    await queue.bind(exchange, routing_key="gadget.updated.v1")
    try:
        yield exchange, queue_name
    finally:
        await queue.delete()
        await connection.close()


async def test_consumer_dedupes_and_dispatches_by_type(
    session_factory: async_sessionmaker[AsyncSession], test_exchange_and_queue, read_metric
) -> None:
    exchange, queue_name = test_exchange_and_queue
    consumer_name = f"test.consumer.{uuid4().hex[:8]}"

    handled: list[dict] = []

    async def handle_created(session: AsyncSession, envelope: dict) -> int:
        handled.append(envelope)
        return 99

    on_handled_values: list[int] = []

    async def on_handled(value: int) -> None:
        on_handled_values.append(value)

    consumer = InboxConsumer(
        consumer_name=consumer_name,
        session_factory=session_factory,
        amqp_url=_AMQP_URL,
        queue_name=queue_name,
        handlers={"gadget.created": handle_created},
        inbox_table="test_consumer_inbox",
        on_handled=on_handled,
    )
    await consumer.start()
    try:
        envelope = build_envelope(
            event_type="gadget.created",
            source="test",
            subject="g1",
            data={},
            schema_version="1.0",
            aggregate_type="gadget",
            aggregate_version=1,
        )
        message = aio_pika.Message(
            body=json.dumps(envelope).encode(), message_id=envelope["id"], type=envelope["type"]
        )

        # First delivery: handled once.
        await exchange.publish(message, routing_key="gadget.created.v1")
        await asyncio.sleep(0.5)

        # Redelivery of the same message_id: must NOT be handled again.
        await exchange.publish(message, routing_key="gadget.created.v1")
        await asyncio.sleep(0.5)

        # Unhandled type arriving via a wildcard-style binding: no-op, not an error.
        envelope2 = build_envelope(
            event_type="gadget.updated",
            source="test",
            subject="g2",
            data={},
            schema_version="1.0",
            aggregate_type="gadget",
            aggregate_version=1,
        )
        await exchange.publish(
            aio_pika.Message(
                body=json.dumps(envelope2).encode(),
                message_id=envelope2["id"],
                type=envelope2["type"],
            ),
            routing_key="gadget.updated.v1",
        )
        await asyncio.sleep(0.5)
    finally:
        await consumer.stop()

    assert len(handled) == 1
    assert handled[0]["id"] == envelope["id"]
    assert on_handled_values == [99]

    # LLD-08 §13: an unhandled type is still acked and committed -- processed.
    by = dict(consumer=consumer_name)
    total = "consumer_messages_total"
    assert read_metric(total, **by, type="gadget.created", result="processed") == 1
    assert read_metric(total, **by, type="gadget.created", result="duplicate") == 1
    assert read_metric(total, **by, type="gadget.updated", result="processed") == 1
    assert read_metric("consumer_processing_duration_seconds", **by) == 3


async def test_consumer_nacks_without_requeue_on_handler_exception(
    session_factory: async_sessionmaker[AsyncSession], test_exchange_and_queue, read_metric
) -> None:
    exchange, queue_name = test_exchange_and_queue
    consumer_name = f"test.consumer.{uuid4().hex[:8]}"
    attempts = 0

    async def handle_created(session: AsyncSession, envelope: dict) -> None:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("boom")

    consumer = InboxConsumer(
        consumer_name=consumer_name,
        session_factory=session_factory,
        amqp_url=_AMQP_URL,
        queue_name=queue_name,
        handlers={"gadget.created": handle_created},
        inbox_table="test_consumer_inbox",
    )
    await consumer.start()
    try:
        envelope = build_envelope(
            event_type="gadget.created",
            source="test",
            subject="g3",
            data={},
            schema_version="1.0",
            aggregate_type="gadget",
            aggregate_version=1,
        )
        await exchange.publish(
            aio_pika.Message(
                body=json.dumps(envelope).encode(), message_id=envelope["id"], type=envelope["type"]
            ),
            routing_key="gadget.created.v1",
        )
        await asyncio.sleep(0.5)
    finally:
        await consumer.stop()

    # requeue=False means no redelivery to this queue; the handler ran exactly once,
    # not repeatedly (which a requeue=True nack would have caused).
    assert attempts == 1
    assert (
        read_metric(
            "consumer_messages_total",
            consumer=consumer_name,
            type="gadget.created",
            result="dead_lettered",
        )
        == 1
    )
