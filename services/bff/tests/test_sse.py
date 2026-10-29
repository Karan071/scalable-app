"""Unit tests for SSE order-status fan-out (LLD-06 §10). No live Redis/
RabbitMQ needed -- both are faked, same style as test_event_consumer.py's
own fake redis/message doubles.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest
from app.main import app as real_app
from app.sse import CONSUMER_NAME, PerUserStreamLimiter, SseFanoutConsumer
from fastapi.testclient import TestClient
from test_csrf_routes import FakeSessions, _record


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.published: list[tuple[str, str]] = []

    async def set(self, key: str, value: str, *, nx: bool = False, ex: int | None = None) -> bool:
        if nx and key in self.store:
            return False
        self.store[key] = value
        return True

    async def publish(self, channel: str, message: str) -> None:
        self.published.append((channel, message))


def _message(envelope: dict, *, message_id: str = "msg-1") -> SimpleNamespace:
    acked = {"ack": False, "nacked": False}

    async def ack() -> None:
        acked["ack"] = True

    async def nack(*, requeue: bool = False) -> None:
        acked["nacked"] = True

    return SimpleNamespace(
        body=json.dumps(envelope).encode(), message_id=message_id, ack=ack, nack=nack, _acked=acked
    )


def _consumer(redis: FakeRedis) -> SseFanoutConsumer:
    return SseFanoutConsumer(redis=redis, amqp_url="amqp://unused", queue_name="q.bff.sse")  # type: ignore[arg-type]


def _status_changed_envelope(
    *, customer_id: str = "cust-1", envelope_id: str = "evt-1", to: str = "confirmed"
) -> dict:
    return {
        "id": envelope_id,
        "type": "order.status_changed",
        "data": {"order_id": "order-1", "customer_id": customer_id, "from": "pending", "to": to},
    }


async def test_order_status_changed_is_published_to_the_customers_channel() -> None:
    redis = FakeRedis()
    consumer = _consumer(redis)
    message = _message(_status_changed_envelope(customer_id="cust-42"))

    await consumer._on_message(message)

    assert len(redis.published) == 1
    channel, payload = redis.published[0]
    assert channel == "sse:user:cust-42"
    decoded = json.loads(payload)
    assert decoded["type"] == "order_status"
    assert decoded["event_id"] == "evt-1"
    assert decoded["data"]["to"] == "confirmed"
    assert message._acked["ack"] is True


async def test_duplicate_message_id_is_not_republished() -> None:
    redis = FakeRedis()
    consumer = _consumer(redis)
    envelope = _status_changed_envelope(envelope_id="evt-dup")

    await consumer._on_message(_message(envelope))
    await consumer._on_message(_message(envelope))

    assert len(redis.published) == 1


async def test_unrelated_event_type_is_a_noop_but_still_acked() -> None:
    redis = FakeRedis()
    consumer = _consumer(redis)
    message = _message({"id": "evt-2", "type": "notification.in_app", "data": {}})

    await consumer._on_message(message)

    assert redis.published == []
    assert message._acked["ack"] is True


async def test_malformed_message_is_nacked_without_requeue() -> None:
    redis = FakeRedis()
    consumer = _consumer(redis)
    message = SimpleNamespace(body=b"not json", message_id="bad-1", ack=pytest.fail, _acked={})

    async def nack(*, requeue: bool = False) -> None:
        message._acked["nacked"] = True
        message._acked["requeue"] = requeue

    message.nack = nack

    await consumer._on_message(message)

    assert message._acked.get("nacked") is True
    assert message._acked.get("requeue") is False


def test_consumer_name_is_stable() -> None:
    assert CONSUMER_NAME == "bff.sse"


async def test_per_user_stream_limiter_caps_concurrent_streams() -> None:
    limiter = PerUserStreamLimiter(max_per_user=3)

    for _ in range(3):
        assert await limiter.try_acquire("user-1") is True
    assert await limiter.try_acquire("user-1") is False


async def test_per_user_stream_limiter_tracks_users_independently() -> None:
    limiter = PerUserStreamLimiter(max_per_user=1)

    assert await limiter.try_acquire("user-1") is True
    assert await limiter.try_acquire("user-2") is True


async def test_per_user_stream_limiter_release_frees_a_slot() -> None:
    limiter = PerUserStreamLimiter(max_per_user=1)
    await limiter.try_acquire("user-1")

    await limiter.release("user-1")

    assert await limiter.try_acquire("user-1") is True


class FakePubSub:
    def __init__(self, messages: list[dict]) -> None:
        self._messages = list(messages)
        self.subscribed: list[str] = []
        self.closed = False

    async def subscribe(self, channel: str) -> None:
        self.subscribed.append(channel)

    async def get_message(
        self, *, ignore_subscribe_messages: bool = True, timeout: float = 0.0
    ) -> dict | None:
        if self._messages:
            return self._messages.pop(0)
        await asyncio.sleep(10)  # the test ends the stream before this returns
        return None

    async def unsubscribe(self, channel: str) -> None:
        pass

    async def aclose(self) -> None:
        self.closed = True


class FakeRedisCache:
    def __init__(self, pubsub: FakePubSub) -> None:
        self._pubsub = pubsub

    def pubsub(self) -> FakePubSub:
        return self._pubsub


def _settings() -> SimpleNamespace:
    return SimpleNamespace(cookie_secure=True, session_idle_standard_s=1800)


def test_events_route_requires_a_session() -> None:
    real_app.state.settings = _settings()
    real_app.state.sessions = FakeSessions(None)
    client = TestClient(real_app, base_url="https://app.test")

    resp = client.get("/api/events")

    assert resp.status_code == 401


def test_events_route_rejects_a_4th_concurrent_stream_for_the_same_user() -> None:
    """A 429 is returned before any stream is opened (no StreamingResponse
    involved), so this is safe to drive through TestClient -- unlike an
    actually-open stream (``_stream``'s own tests below), which an
    in-process ASGI test client has no clean way to cancel mid-read."""
    real_app.state.settings = _settings()
    real_app.state.sessions = FakeSessions(_record())
    limiter = PerUserStreamLimiter(max_per_user=3)
    real_app.state.sse_stream_limiter = limiter
    client = TestClient(real_app, base_url="https://app.test")
    client.cookies.set("__Host-sf_session", "test-session-id")

    asyncio.run(limiter.try_acquire("user-1"))
    asyncio.run(limiter.try_acquire("user-1"))
    asyncio.run(limiter.try_acquire("user-1"))

    resp = client.get("/api/events", headers={"accept": ""})

    assert resp.status_code == 429


async def test_stream_yields_a_formatted_event_then_cleans_up_on_close() -> None:
    from app.sse import _stream

    pubsub = FakePubSub(
        [
            {
                "data": json.dumps(
                    {"event_id": "evt-1", "type": "order_status", "data": {"to": "confirmed"}}
                )
            }
        ]
    )
    redis = FakeRedisCache(pubsub)
    limiter = PerUserStreamLimiter(max_per_user=3)
    await limiter.try_acquire("user-1")

    gen = _stream(redis, "user-1", limiter)  # type: ignore[arg-type]
    chunk = await gen.__anext__()

    assert chunk == b'id: evt-1\nevent: order_status\ndata: {"to": "confirmed"}\n\n'
    assert pubsub.subscribed == ["sse:user:user-1"]

    await gen.aclose()

    assert pubsub.closed is True
    assert await limiter.try_acquire("user-1") is True  # the slot _stream held was released


async def test_stream_emits_a_heartbeat_when_no_message_arrives() -> None:
    from app.sse import _stream

    pubsub = FakePubSub([])  # every get_message() call times out (returns None)
    pubsub.get_message = _immediate_none  # type: ignore[method-assign]
    redis = FakeRedisCache(pubsub)
    limiter = PerUserStreamLimiter(max_per_user=3)

    gen = _stream(redis, "user-1", limiter)  # type: ignore[arg-type]
    chunk = await gen.__anext__()

    assert chunk == b": heartbeat\n\n"
    await gen.aclose()


async def _immediate_none(*, ignore_subscribe_messages: bool = True, timeout: float = 0.0) -> None:
    return None
