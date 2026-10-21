"""Unit tests for the identity event consumer (LLD-01 §8). No live
Redis/RabbitMQ needed -- both are faked, same style as test_proxy.py's
fake redis/sessions doubles.
"""

import json
from types import SimpleNamespace

import pytest
from app.event_consumer import CONSUMER_NAME, IdentityEventConsumer


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def set(self, key: str, value: str, *, nx: bool = False, ex: int | None = None) -> bool:
        if nx and key in self.store:
            return False
        self.store[key] = value
        return True


class FakeSessions:
    def __init__(self) -> None:
        self.revoked_for: list[str] = []

    async def delete_all_for_user(self, uid: str) -> int:
        self.revoked_for.append(uid)
        return 2


def _message(envelope: dict, *, message_id: str = "msg-1") -> SimpleNamespace:
    acked = {"ack": False, "nacked": False}

    async def ack() -> None:
        acked["ack"] = True

    async def nack(*, requeue: bool = False) -> None:
        acked["nacked"] = True

    return SimpleNamespace(
        body=json.dumps(envelope).encode(),
        message_id=message_id,
        ack=ack,
        nack=nack,
        _acked=acked,
    )


def _consumer(redis: FakeRedis, sessions: FakeSessions) -> IdentityEventConsumer:
    return IdentityEventConsumer(
        redis=redis,  # type: ignore[arg-type]
        sessions=sessions,  # type: ignore[arg-type]
        amqp_url="amqp://unused",
        queue_name="q.bff.identity",
    )


def _user_disabled_envelope(user_id: str = "user-1", envelope_id: str = "evt-1") -> dict:
    return {"id": envelope_id, "type": "user.disabled", "data": {"user_id": user_id}}


async def test_user_disabled_revokes_all_sessions_for_that_user() -> None:
    redis, sessions = FakeRedis(), FakeSessions()
    consumer = _consumer(redis, sessions)
    message = _message(_user_disabled_envelope(user_id="user-42"))

    await consumer._on_message(message)

    assert sessions.revoked_for == ["user-42"]
    assert message._acked["ack"] is True
    assert message._acked["nacked"] is False


async def test_duplicate_message_id_is_not_reprocessed() -> None:
    redis, sessions = FakeRedis(), FakeSessions()
    consumer = _consumer(redis, sessions)
    envelope = _user_disabled_envelope(envelope_id="evt-dup")

    await consumer._on_message(_message(envelope))
    await consumer._on_message(_message(envelope))

    assert sessions.revoked_for == ["user-1"]  # only once


async def test_unrelated_event_type_is_a_noop_but_still_acked() -> None:
    redis, sessions = FakeRedis(), FakeSessions()
    consumer = _consumer(redis, sessions)
    message = _message({"id": "evt-2", "type": "user.reactivated", "data": {"user_id": "user-1"}})

    await consumer._on_message(message)

    assert sessions.revoked_for == []
    assert message._acked["ack"] is True


async def test_malformed_message_is_nacked_without_requeue() -> None:
    redis, sessions = FakeRedis(), FakeSessions()
    consumer = _consumer(redis, sessions)
    message = SimpleNamespace(
        body=b"not json",
        message_id="bad-1",
        ack=pytest.fail,  # must never be called
        _acked={},
    )

    async def nack(*, requeue: bool = False) -> None:
        message._acked["nacked"] = True
        message._acked["requeue"] = requeue

    message.nack = nack

    await consumer._on_message(message)

    assert message._acked.get("nacked") is True
    assert message._acked.get("requeue") is False


def test_consumer_name_is_stable() -> None:
    assert CONSUMER_NAME == "bff.identity"
