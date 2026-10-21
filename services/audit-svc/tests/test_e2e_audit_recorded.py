"""Genuine end-to-end proof of the E2.9 chain: publish a real
``audit.recorded`` envelope onto the real ``q.audit.ingest`` queue against
real local RabbitMQ, let a real ``InboxConsumer`` (wired to the real
``event_handlers.DISPATCH`` table) process it against the real local
Postgres, then confirm the row landed with an intact hash chain — proving
publish -> broker -> consumer -> append-only insert without needing a
second running service.
"""

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import aio_pika
import pytest
import pytest_asyncio
from sf_messaging import InboxConsumer, build_envelope
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_AMQP_URL = os.environ.get(
    "SF_TEST_AMQP_URL",
    f"amqp://{os.environ.get('SF_RABBITMQ_USER', 'scaleforge')}:"
    f"{os.environ.get('SF_RABBITMQ_PASSWORD', 'rabbitmq_local_dev')}@localhost:5672/",
)


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    try:
        from app.db import build_audit_engine, build_audit_session_factory
        from app.settings import Settings
    except Exception as exc:
        pytest.skip(f"could not import app modules: {exc}")
        return

    engine = build_audit_engine(Settings())
    factory = build_audit_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(select(text("1")))
    except Exception as exc:
        await engine.dispose()
        pytest.skip(f"local Postgres not reachable: {exc}")
        return
    try:
        yield factory
    finally:
        await engine.dispose()


async def test_audit_recorded_flows_through_the_real_queue_to_a_real_insert(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app import event_handlers, repository

    try:
        connection = await aio_pika.connect_robust(_AMQP_URL, timeout=3)
    except Exception as exc:
        pytest.skip(f"local RabbitMQ not reachable: {exc}")
        return

    audit_id = uuid.uuid4()
    org_id = uuid.uuid4()
    envelope = build_envelope(
        event_type="audit.recorded",
        source="orgs-svc",
        subject=str(audit_id),
        data={
            "audit_id": str(audit_id),
            "occurred_at": datetime.now(UTC).isoformat(),
            "actor": {"type": "user", "id": str(uuid.uuid4())},
            "domain": f"org:{org_id}",
            "action": "membership.created",
            "target": {"type": "membership", "id": str(uuid.uuid4())},
            "result": "success",
            "source": {"service": "orgs-svc", "version": "0.1.0"},
            "severity": "info",
        },
        schema_version="1.0",
        aggregate_type="audit_event",
        aggregate_version=1,
    )

    consumer = InboxConsumer(
        consumer_name=f"test.{event_handlers.CONSUMER_NAME}.{uuid.uuid4().hex[:8]}",
        session_factory=session_factory,
        amqp_url=_AMQP_URL,
        queue_name="q.audit.ingest",
        handlers=event_handlers.DISPATCH,
    )
    await consumer.start()
    try:
        channel = await connection.channel()
        exchange = await channel.declare_exchange(
            "sf.events", type="topic", durable=True, passive=True
        )
        await exchange.publish(
            aio_pika.Message(
                body=json.dumps(envelope).encode(),
                message_id=envelope["id"],
                type=envelope["type"],
            ),
            routing_key="audit.recorded.v1",
        )

        # Poll for *this specific* audit_id landing, not just "the consumer
        # did something" — a shared queue could have unrelated backlog.
        landed = False
        for _ in range(50):
            async with session_factory() as session:
                rows = await repository.list_audit_events(session, domain=f"org:{org_id}", limit=10)
            if any(str(r.id) == str(audit_id) for r in rows):
                landed = True
                break
            await asyncio.sleep(0.1)
        assert landed, "consumer never processed the published message into a row"
    finally:
        await consumer.stop()
        await connection.close()

    # Confirm the chain is still intact after this real insert.
    async with session_factory() as session:
        ok, broken_at = await repository.verify_chain(session)
    assert ok, f"chain broken at seq {broken_at}"
