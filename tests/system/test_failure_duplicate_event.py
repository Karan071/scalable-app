"""E4.8 / LLD-13 §3 FS-5: duplicate event delivery is a no-op.

    make test-system

A real audit-svc process, real RabbitMQ, the fake IdP. The same
``audit.recorded.v1`` envelope (same ``message_id`` *and* the same
producer-generated ``audit_id`` the envelope's own ``data`` carries) is
published twice -- a crashed-before-ack consumer redelivering, or a replay
tool resending, look identical on the wire (LLD-13 §3's own FS-5 injection:
"republish N random messages ... + consumer crash before ack").

``audit_events.id`` is the ``audit_id`` (``AuditEvent``'s own docstring:
"the dedupe key from the envelope payload"), and
``repository.insert_audit_event`` is documented to raise ``IntegrityError``
on a duplicate one -- so this is a real, not incidental, test of
``InboxConsumer``'s own ``inbox`` table dedup (LLD-08 §6): if that dedup
didn't work, the second delivery wouldn't just be "slightly wasteful", it
would error and dead-letter. inventory-svc's own catalog consumer was
considered for this instead, but its one handler (``upsert_new_stock_item``)
is independently idempotent via ``ON CONFLICT DO NOTHING`` -- it can't tell
"dedup worked" apart from "the handler happened to be idempotent anyway".
"""

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import uuid4

import aio_pika
import asyncpg
import pytest
import pytest_asyncio
from harness import (
    ENV,
    LOG_DIR,
    TEST_DB,
    TEST_VHOST,
    FakeIdP,
    ServiceProcess,
    service_env,
    wait_until,
)
from sf_messaging import build_envelope

pytestmark = pytest.mark.asyncio(loop_scope="module")

_PORT = 18171


async def _db() -> asyncpg.Connection:
    return await asyncpg.connect(
        host="localhost", database=TEST_DB, user="svc_audit", password=ENV["POSTGRES_APP_PASSWORD"]
    )


async def _audit_event_count(audit_id: str) -> int:
    conn = await _db()
    try:
        return await conn.fetchval("SELECT count(*) FROM audit_events WHERE id = $1", audit_id)
    finally:
        await conn.close()


async def _inbox_count(message_id: str) -> int:
    conn = await _db()
    try:
        return await conn.fetchval(
            "SELECT count(*) FROM inbox WHERE consumer = 'audit.ingest' AND message_id = $1",
            message_id,
        )
    finally:
        await conn.close()


_LABEL = "fs5-audit"


@dataclass
class Fixture:
    base_url: str


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def fixture() -> AsyncIterator[Fixture]:
    try:
        conn = await _db()
        await conn.close()
    except Exception as exc:
        pytest.skip(f"local Postgres not reachable: {exc}")

    idp = FakeIdP()
    process = ServiceProcess(
        "audit-svc", port=_PORT, label=_LABEL, env=service_env("audit-svc", idp)
    )
    try:
        await process.start()
        yield Fixture(base_url=process.base_url)
    finally:
        process.stop()
        idp.stop()


async def _publish_audit_recorded(envelope: dict, *, times: int) -> None:
    url = (
        f"amqp://{ENV['SF_RABBITMQ_USER']}:{ENV['SF_RABBITMQ_PASSWORD']}"
        f"@localhost:5672/{TEST_VHOST}"
    )
    connection = await aio_pika.connect_robust(url)
    try:
        channel = await connection.channel()
        exchange = await channel.get_exchange("sf.events")
        body = json.dumps(envelope).encode()
        for _ in range(times):
            await exchange.publish(
                aio_pika.Message(body, message_id=envelope["id"]),
                routing_key="audit.recorded.v1",
            )
    finally:
        await connection.close()


async def test_a_redelivered_audit_event_is_recorded_exactly_once(fixture: Fixture) -> None:
    audit_id = str(uuid4())
    envelope = build_envelope(
        event_type="audit.recorded",
        source="catalog-svc",
        subject=audit_id,
        data={
            "audit_id": audit_id,
            "actor": {"type": "user", "id": str(uuid4())},
            "domain": "org:test",
            "action": "product.updated",
            "target": {"type": "product", "id": str(uuid4())},
            "result": "success",
            "source": {"service": "catalog-svc"},
            "severity": "info",
        },
        schema_version="1.0",
        aggregate_type="audit_event",
        aggregate_version=1,
    )

    # Same message_id, same audit_id, twice -- redelivery/replay, not two
    # distinct audit events.
    await _publish_audit_recorded(envelope, times=2)

    async def processed() -> bool:
        return await _inbox_count(envelope["id"]) == 1

    await wait_until(processed, timeout_s=10.0)

    assert await _audit_event_count(audit_id) == 1
    assert await _inbox_count(envelope["id"]) == 1

    log_text = (LOG_DIR / f"{_LABEL}.log").read_text()
    assert "IntegrityError" not in log_text, "the second delivery must never reach the handler"
    assert "Traceback" not in log_text
