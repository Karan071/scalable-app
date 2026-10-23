"""Watcher tests: fake async pubsub + a stub enforcer recording reload() calls."""

import asyncio

import pytest
from sf_authz.watcher import PolicyWatcher


class _FakePubSub:
    def __init__(self, queue: asyncio.Queue) -> None:
        self._queue = queue

    async def subscribe(self, _channel: str) -> None:
        return None

    async def listen(self):
        while True:
            message = await self._queue.get()
            yield message

    async def aclose(self) -> None:
        return None


class _FakeRedis:
    def __init__(self) -> None:
        self.queue: asyncio.Queue = asyncio.Queue()

    def pubsub(self) -> _FakePubSub:
        return _FakePubSub(self.queue)

    async def publish(self, version: int) -> None:
        await self.queue.put({"type": "message", "data": str(version)})


class _StubEnforcer:
    def __init__(self, *, initial_version: int = 1, reload_delay_s: float = 0.0) -> None:
        self.current_version = initial_version
        self.reload_calls: list[int] = []
        self.confirmations: list[bool] = []
        self._reload_delay_s = reload_delay_s
        self._next_version = initial_version

    async def reload(self) -> int:
        self.reload_calls.append(self.current_version)
        if self._reload_delay_s:
            await asyncio.sleep(self._reload_delay_s)
        self.current_version = self._next_version
        return self.current_version

    async def fetch_version_only(self) -> int:
        return self._next_version

    def set_next_version(self, version: int) -> None:
        self._next_version = version

    def confirm_current(self, version: int) -> bool:
        confirmed = version == self.current_version
        self.confirmations.append(confirmed)
        return confirmed


@pytest.mark.asyncio
async def test_redis_signal_triggers_a_single_debounced_reload() -> None:
    redis = _FakeRedis()
    enforcer = _StubEnforcer(initial_version=1)
    enforcer.set_next_version(2)
    watcher = PolicyWatcher(
        enforcer=enforcer, redis_client=redis, debounce_s=0.05, poll_interval_s=100
    )
    await watcher.start()

    for _ in range(5):
        await redis.publish(2)
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.15)

    await watcher.stop()
    assert len(enforcer.reload_calls) == 1


@pytest.mark.asyncio
async def test_signal_for_a_version_not_newer_than_current_is_ignored() -> None:
    redis = _FakeRedis()
    enforcer = _StubEnforcer(initial_version=5)
    watcher = PolicyWatcher(
        enforcer=enforcer, redis_client=redis, debounce_s=0.02, poll_interval_s=100
    )
    await watcher.start()

    await redis.publish(5)
    await asyncio.sleep(0.1)

    await watcher.stop()
    assert enforcer.reload_calls == []


@pytest.mark.asyncio
async def test_signal_during_in_flight_reload_does_not_spawn_a_second_one() -> None:
    redis = _FakeRedis()
    enforcer = _StubEnforcer(initial_version=1, reload_delay_s=0.2)
    enforcer.set_next_version(2)
    watcher = PolicyWatcher(
        enforcer=enforcer, redis_client=redis, debounce_s=0.02, poll_interval_s=100
    )
    await watcher.start()

    await redis.publish(2)
    await asyncio.sleep(0.05)  # reload now in flight (0.2s delay)
    await redis.publish(2)  # same version again while in flight: must not start a 2nd reload
    await asyncio.sleep(0.3)

    await watcher.stop()
    assert len(enforcer.reload_calls) == 1


@pytest.mark.asyncio
async def test_poll_loop_alone_triggers_reload_without_redis() -> None:
    enforcer = _StubEnforcer(initial_version=1)
    enforcer.set_next_version(2)
    watcher = PolicyWatcher(
        enforcer=enforcer, redis_client=None, poll_interval_s=0.05, debounce_s=0.01
    )
    await watcher.start()

    await asyncio.sleep(0.2)

    await watcher.stop()
    assert len(enforcer.reload_calls) >= 1


@pytest.mark.asyncio
async def test_a_poll_at_the_current_version_confirms_freshness_without_reloading() -> None:
    """LLD-02 §8. Without this, a quiet system drifted into authz_stale after
    60 s (every mutating request 503) and failed readiness after 300 s."""
    enforcer = _StubEnforcer(initial_version=3)
    watcher = PolicyWatcher(
        enforcer=enforcer, redis_client=None, poll_interval_s=0.05, debounce_s=0.01
    )
    await watcher.start()

    await asyncio.sleep(0.2)

    await watcher.stop()
    assert enforcer.confirmations and all(enforcer.confirmations)
    assert enforcer.reload_calls == []


@pytest.mark.asyncio
async def test_a_poll_at_a_newer_version_reloads_instead_of_confirming() -> None:
    enforcer = _StubEnforcer(initial_version=3)
    enforcer.set_next_version(4)
    watcher = PolicyWatcher(
        enforcer=enforcer, redis_client=None, poll_interval_s=0.05, debounce_s=0.01
    )
    await watcher.start()

    await asyncio.sleep(0.03)  # first poll fires at ~0.05 s
    assert enforcer.reload_calls == []
    await asyncio.sleep(0.15)

    await watcher.stop()
    assert enforcer.reload_calls  # behind -> reload
    assert enforcer.confirmations[0] is False  # the poll that found v4 confirmed nothing
