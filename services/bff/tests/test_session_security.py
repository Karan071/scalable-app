"""Session security suite (LLD-01 §16, LLD-14 §4 "Session" row):
fixation, idle/absolute expiry, logout revocation, disabled-user
revocation latency. Against real local redis-session; skips if
unreachable (house convention).
"""

import asyncio
import time
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from app.sessions import SessionStore
from redis.asyncio import Redis
from sf_common import TokenCipher

_KEYS = {"k1": "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="}


@pytest_asyncio.fixture
async def store() -> AsyncIterator[SessionStore]:
    redis: Redis = Redis.from_url("redis://localhost:6379/0", decode_responses=True)
    try:
        await redis.ping()
    except Exception as exc:
        pytest.skip(f"local redis-session not reachable: {exc}")
        return
    cipher = TokenCipher.from_base64_keys(_KEYS, "k1")
    try:
        yield SessionStore(redis, cipher)
    finally:
        await redis.aclose()


def _create_kwargs(**overrides: object) -> dict:
    now = int(time.time())
    base = dict(
        uid="user-1",
        sub="sub-1",
        idp_sid="idp-sid-1",
        access_token="at-1",
        refresh_token="rt-1",
        id_token="idt-1",
        at_exp=now + 300,
        acr="1",
        auth_time=now,
        session_class="standard",
        idle_s=1800,
        abs_s=43200,
    )
    base.update(overrides)
    return base


async def test_fixation_each_login_mints_a_fresh_random_session_id(store: SessionStore) -> None:
    """An attacker who sets a session cookie on the victim's browser before
    login must not have it "become" the victim's session -- login always
    calls create(), which mints a new cryptographically random ID, never
    reusing whatever cookie value was already present."""
    first_id, _ = await store.create(**_create_kwargs())
    second_id, _ = await store.create(**_create_kwargs())

    assert first_id != second_id
    assert len(first_id) >= 32  # base64url of 32 random bytes, not a short/guessable value


async def test_idle_expiry_is_enforced_via_the_redis_ttl(store: SessionStore) -> None:
    """Idle expiry isn't an explicit check inside load() -- it's the Redis
    key's own TTL (set to idle_s at create() time). Once it elapses, Redis
    deletes the key itself, and load() sees an empty hash -> None."""
    session_id, _ = await store.create(**_create_kwargs(idle_s=1))

    assert await store.load(session_id, idle_s=1) is not None
    await asyncio.sleep(1.5)

    assert await store.load(session_id, idle_s=1) is None


async def test_absolute_expiry_deletes_the_session_past_abs_exp(store: SessionStore) -> None:
    session_id, _ = await store.create(**_create_kwargs(idle_s=1800, abs_s=1))
    await asyncio.sleep(1.5)

    record = await store.load(session_id, idle_s=1800)

    assert record is None
    # Deleted, not just "expired looking" -- a second load must also be None.
    assert await store.load(session_id, idle_s=1800) is None


async def test_logout_deletes_the_session_so_any_replica_rejects_it(store: SessionStore) -> None:
    """Both BFF replicas read the same Redis, so "logout on A rejects on
    B" (LLD-01 §16) reduces to: after delete(), any subsequent load() --
    from any process -- returns None. One SessionStore instance here
    stands in for both replicas, since they share nothing but Redis."""
    session_id, _ = await store.create(**_create_kwargs())
    assert await store.load(session_id, idle_s=1800) is not None

    await store.delete(session_id)

    assert await store.load(session_id, idle_s=1800) is None


async def test_logout_all_revokes_every_session_for_that_user(store: SessionStore) -> None:
    uid = "user-logout-all"
    id_a, _ = await store.create(**_create_kwargs(uid=uid))
    id_b, _ = await store.create(**_create_kwargs(uid=uid))
    other_id, _ = await store.create(**_create_kwargs(uid="someone-else"))

    revoked = await store.delete_all_for_user(uid)

    assert revoked == 2
    assert await store.load(id_a, idle_s=1800) is None
    assert await store.load(id_b, idle_s=1800) is None
    assert await store.load(other_id, idle_s=1800) is not None


async def test_disabled_user_revocation_latency_via_the_identity_consumer(
    store: SessionStore,
) -> None:
    """LLD-01 §8's "<=5s target": the consumer's own work (a redis SETNX
    dedup check + delete_all_for_user) is sub-millisecond against local
    Redis -- this asserts the mechanism is correct and fast, not a 5s
    wall-clock race against a real broker (that path is covered by the
    unit tests in test_event_consumer.py with a faked broker message)."""
    from app.event_consumer import IdentityEventConsumer

    uid = "user-to-disable"
    id_a, _ = await store.create(**_create_kwargs(uid=uid))
    id_b, _ = await store.create(**_create_kwargs(uid=uid))

    consumer = IdentityEventConsumer(
        redis=store._redis,  # type: ignore[attr-defined]
        sessions=store,
        amqp_url="amqp://unused",
        queue_name="q.bff.identity",
    )

    import json
    import uuid
    from types import SimpleNamespace

    # A fresh ID every run: the consumer's dedup key lives in the same real
    # Redis across separate test invocations (by design, 24h TTL), so a
    # hardcoded id would silently no-op on a second local run.
    event_id = f"evt-disable-{uuid.uuid4()}"
    envelope = {"id": event_id, "type": "user.disabled", "data": {"user_id": uid}}
    message = SimpleNamespace(
        body=json.dumps(envelope).encode(),
        message_id=event_id,
        ack=lambda: _noop(),
        nack=lambda requeue=False: _noop(),
    )

    started = time.monotonic()
    await consumer._on_message(message)
    elapsed_s = time.monotonic() - started

    assert elapsed_s < 5.0
    assert await store.load(id_a, idle_s=1800) is None
    assert await store.load(id_b, idle_s=1800) is None


async def _noop() -> None:
    pass
