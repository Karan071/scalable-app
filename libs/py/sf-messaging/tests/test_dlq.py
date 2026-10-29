"""Integration tests against the local Compose Postgres + RabbitMQ (LLD-08
§7.1, E4.5): ``build_dlq_router``'s peek/replay/discard, through a real
FastAPI app and a real broker -- not fakes, since the thing worth getting
wrong here is exactly the get/nack/ack sequencing against a real queue.
"""

import os
from collections.abc import AsyncIterator
from uuid import uuid4

import aio_pika
import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from sf_db import build_engine, build_session_factory
from sf_messaging import build_dlq_router
from sf_messaging.retry import ATTEMPT_HEADER, FIRST_FAILED_HEADER
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_DB_PASSWORD = os.environ.get("SF_TEST_DB_PASSWORD", "app_local_dev")
_AMQP_URL = os.environ["SF_TEST_AMQP_URL"]


class _FakePrincipal:
    subject = "test-ops-client"


def _noop_auth() -> None:
    return None


def _fake_principal_dependency(_request: Request) -> _FakePrincipal:
    return _FakePrincipal()


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    # No TEMP TABLE shadow here (unlike test_outbox.py): build_dlq_router's
    # own audit write opens a *new* session per call, which a pooled engine
    # may hand a different physical connection than created any temp table
    # -- a real table, cleaned up by aggregate_id below, sidesteps that.
    engine = build_engine(
        host="localhost",
        port=5432,
        database=os.environ["SF_TEST_DB_NAME"],
        user="svc_users",
        password=_DB_PASSWORD,
    )
    try:
        factory = build_session_factory(engine)
        async with factory() as s:
            await s.execute(text("SELECT 1 FROM outbox LIMIT 1"))
    except Exception as exc:
        await engine.dispose()
        pytest.skip(f"local Postgres not reachable: {exc}")
        return
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def rabbit() -> AsyncIterator[
    tuple[aio_pika.abc.AbstractConnection, aio_pika.abc.AbstractChannel, str]
]:
    try:
        connection = await aio_pika.connect_robust(_AMQP_URL, timeout=3)
    except Exception as exc:
        pytest.skip(f"local RabbitMQ not reachable: {exc}")
        return
    channel = await connection.channel()
    queue_name = f"q.test.dlqrouter-{uuid4().hex[:12]}"
    queue = await channel.declare_queue(queue_name, durable=True)
    dlq = await channel.declare_queue(f"{queue_name}.dlq", durable=True)
    try:
        yield connection, channel, queue_name
    finally:
        # if_empty=False: several tests here leave messages behind by
        # design (peek doesn't consume; discard/replay only touch the
        # messages they selected) -- cleanup must still work regardless.
        await queue.delete(if_empty=False)
        await dlq.delete(if_empty=False)
        await connection.close()


@pytest_asyncio.fixture
async def app_client(
    session_factory: async_sessionmaker[AsyncSession],
    rabbit: tuple[aio_pika.abc.AbstractConnection, aio_pika.abc.AbstractChannel, str],
) -> AsyncIterator[httpx.AsyncClient]:
    # An in-process ASGI transport, not `fastapi.testclient.TestClient`:
    # TestClient drives the app from its own internal event loop (an AnyIO
    # portal), which doesn't share asyncpg connections opened on *this*
    # pytest-asyncio loop by `session_factory` -- confirmed by a "Future
    # attached to a different loop" failure the first time this ran.
    _connection, _channel, queue_name = rabbit
    app = FastAPI()
    app.include_router(
        build_dlq_router(
            service_name="test-dlq",
            queue_names=[queue_name],
            amqp_url=_AMQP_URL,
            session_factory=session_factory,
            notify_channel="outbox_test_dlq",
            auth=_noop_auth,
            principal_dependency=_fake_principal_dependency,
        )
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def _publish_to_dlq(
    channel: aio_pika.abc.AbstractChannel,
    queue_name: str,
    *,
    message_id: str,
    body: bytes = b'{"hello": "world"}',
    headers: dict[str, object] | None = None,
) -> None:
    await channel.default_exchange.publish(
        aio_pika.Message(body=body, message_id=message_id, headers=headers or {}),
        routing_key=f"{queue_name}.dlq",
    )


async def _depth(connection: aio_pika.abc.AbstractConnection, name: str) -> int:
    # A *fresh* channel, not the long-lived fixture one: aio_pika caches a
    # channel's own Queue proxy by name, so a passive re-declare on a
    # channel that already declared this queue returns that first
    # declaration's stale message_count instead of asking the broker again
    # -- confirmed directly (a new channel on the same connection reports
    # the live count; the original channel keeps reporting its first-ever
    # value). A dedicated channel per check sidesteps that cache entirely.
    channel = await connection.channel()
    try:
        queue = await channel.declare_queue(name, durable=True, passive=True)
        return queue.declaration_result.message_count or 0
    finally:
        await channel.close()


async def _audit_rows(
    session_factory: async_sessionmaker[AsyncSession], *, queue_name: str
) -> list[dict]:
    async with session_factory() as session:
        rows = await session.execute(
            text("SELECT payload FROM outbox WHERE aggregate_id = :q ORDER BY occurred_at"),
            {"q": queue_name},
        )
        import json

        return [json.loads(r[0]) if isinstance(r[0], str) else r[0] for r in rows]


@pytest_asyncio.fixture(autouse=True)
async def _cleanup_audit_rows(
    session_factory: async_sessionmaker[AsyncSession], rabbit: tuple
) -> AsyncIterator[None]:
    _connection, _channel, queue_name = rabbit
    yield
    async with session_factory() as session, session.begin():
        await session.execute(text("DELETE FROM outbox WHERE aggregate_id = :q"), {"q": queue_name})


async def test_peek_does_not_consume_and_decodes_known_headers(
    app_client: httpx.AsyncClient,
    rabbit: tuple[aio_pika.abc.AbstractConnection, aio_pika.abc.AbstractChannel, str],
) -> None:
    connection, channel, queue_name = rabbit
    mid = str(uuid4())
    await _publish_to_dlq(
        channel,
        queue_name,
        message_id=mid,
        headers={
            "x-sf-error-class": "permanent",
            "x-sf-error": "ValueError: boom",
            "x-sf-attempts": 3,
            FIRST_FAILED_HEADER: "2026-10-29T00:00:00+00:00",
            "x-sf-consumer": "test.consumer",
            "x-sf-original-routing-key": "gadget.created.v1",
        },
    )
    await _publish_to_dlq(channel, queue_name, message_id=str(uuid4()))  # no metadata headers

    resp = await app_client.get(f"/internal/v1/dlq/{queue_name}", params={"limit": 10})
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 2

    with_headers = next(i for i in items if i["message_id"] == mid)
    assert with_headers["error_class"] == "permanent"
    assert with_headers["error"] == "ValueError: boom"
    assert with_headers["attempts"] == 3
    assert with_headers["consumer"] == "test.consumer"
    assert with_headers["original_routing_key"] == "gadget.created.v1"
    assert with_headers["body"] == {"hello": "world"}

    no_headers = next(i for i in items if i["message_id"] != mid)
    assert no_headers["error_class"] is None
    assert no_headers["attempts"] is None

    # Peek must not have consumed anything.
    assert await _depth(connection, f"{queue_name}.dlq") == 2


async def test_peek_rejects_an_unknown_queue(app_client: httpx.AsyncClient) -> None:
    resp = await app_client.get("/internal/v1/dlq/q.not-mine", params={"limit": 10})
    assert resp.status_code == 404


async def test_discard_removes_only_the_selected_message_and_is_audited(
    app_client: httpx.AsyncClient,
    rabbit: tuple[aio_pika.abc.AbstractConnection, aio_pika.abc.AbstractChannel, str],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    connection, channel, queue_name = rabbit
    keep, drop = str(uuid4()), str(uuid4())
    await _publish_to_dlq(channel, queue_name, message_id=keep)
    await _publish_to_dlq(channel, queue_name, message_id=drop)

    resp = await app_client.post(
        f"/internal/v1/dlq/{queue_name}:discard",
        json={"message_ids": [drop], "reason": "known bad event, already fixed forward"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"discarded": 1}

    assert await _depth(connection, f"{queue_name}.dlq") == 1
    remaining = (await app_client.get(f"/internal/v1/dlq/{queue_name}")).json()
    assert [m["message_id"] for m in remaining] == [keep]

    rows = await _audit_rows(session_factory, queue_name=queue_name)
    assert len(rows) == 1
    assert rows[0]["type"] == "audit.recorded"
    assert rows[0]["data"]["action"] == "dlq.discarded"
    assert rows[0]["data"]["actor"]["id"] == "test-ops-client"


async def test_replay_requeues_with_a_reset_attempt_counter_and_is_audited(
    app_client: httpx.AsyncClient,
    rabbit: tuple[aio_pika.abc.AbstractConnection, aio_pika.abc.AbstractChannel, str],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    connection, channel, queue_name = rabbit
    mid = str(uuid4())
    await _publish_to_dlq(
        channel, queue_name, message_id=mid, headers={ATTEMPT_HEADER: 4, "x-sf-error": "boom"}
    )

    resp = await app_client.post(
        f"/internal/v1/dlq/{queue_name}:replay",
        json={"message_ids": [mid], "reason": "downstream fixed, safe to retry"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"replayed": 1}
    assert await _depth(connection, f"{queue_name}.dlq") == 0

    delivered = await channel.get_queue(queue_name)
    message = await delivered.get(no_ack=True, fail=False, timeout=3)
    assert message is not None
    assert message.message_id == mid
    assert message.headers[ATTEMPT_HEADER] == 0
    assert message.headers["x-sf-replayed-by"] == "test-ops-client"

    rows = await _audit_rows(session_factory, queue_name=queue_name)
    assert len(rows) == 1
    assert rows[0]["data"]["action"] == "dlq.replayed"


async def test_replay_all_drains_the_whole_dlq(
    app_client: httpx.AsyncClient,
    rabbit: tuple[aio_pika.abc.AbstractConnection, aio_pika.abc.AbstractChannel, str],
) -> None:
    connection, channel, queue_name = rabbit
    for _ in range(3):
        await _publish_to_dlq(channel, queue_name, message_id=str(uuid4()))

    resp = await app_client.post(
        f"/internal/v1/dlq/{queue_name}:replay",
        json={"all": True, "reason": "redeploy fixed the bug, replay the whole backlog"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"replayed": 3}
    assert await _depth(connection, f"{queue_name}.dlq") == 0
    assert await _depth(connection, queue_name) == 3


async def test_replay_requires_message_ids_or_all(
    app_client: httpx.AsyncClient, rabbit: tuple
) -> None:
    _connection, _channel, queue_name = rabbit
    resp = await app_client.post(f"/internal/v1/dlq/{queue_name}:replay", json={"reason": "x"})
    assert resp.status_code == 422
