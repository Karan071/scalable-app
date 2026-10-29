"""Cache-aside with stampede protection (LLD-09 §6). ``FakeRedis`` below
re-implements ``SET_IF_NEWER_LUA``'s own version-comparison logic in
Python -- same "fake the script's behavior, not a real Lua interpreter"
pattern as bff's ``test_ratelimit.py``/``test_proxy.py``.
"""

import asyncio
import json

import pytest
from sf_common.cache import CacheClient
from sf_common.circuit_breaker import CircuitBreaker


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.fail = False

    async def get(self, key: str) -> str | None:
        if self.fail:
            raise ConnectionError("redis-cache unreachable")
        return self.store.get(key)

    async def set(self, key: str, value: str, *, nx: bool = False, px: int | None = None) -> bool:
        if nx and key in self.store:
            return False
        self.store[key] = value
        return True

    async def delete(self, key: str) -> int:
        return 1 if self.store.pop(key, None) is not None else 0

    async def eval(self, _script: str, _numkeys: int, key: str, *args: object) -> int:
        new_version, payload, _ttl_s = args
        existing = self.store.get(key)
        if existing is not None:
            decoded = json.loads(existing)
            if decoded["v"] > int(new_version):
                return 0
        self.store[str(key)] = str(payload)
        return 1


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _client(redis: FakeRedis, *, clock: FakeClock | None = None) -> CacheClient:
    return CacheClient(
        redis,  # type: ignore[arg-type]
        name="test-cache",
        clock=clock or FakeClock(),
        rng=lambda: 0.5,  # fixed jitter: deterministic TTL math
    )


async def test_hard_miss_loads_and_populates_the_cache() -> None:
    redis = FakeRedis()
    client = _client(redis)
    calls = 0

    async def loader() -> tuple[int, str]:
        nonlocal calls
        calls += 1
        return 1, "value-from-db"

    value = await client.get_or_load("k", loader=loader, ttl_s=300.0)

    assert value == "value-from-db"
    assert calls == 1
    assert await client.get("k") is not None


async def test_a_fresh_hit_never_calls_the_loader() -> None:
    redis = FakeRedis()
    client = _client(redis)
    await client.set_if_newer("k", version=1, value="cached", ttl_s=300.0)
    calls = 0

    async def loader() -> tuple[int, str]:
        nonlocal calls
        calls += 1
        return 2, "should not be called"

    value = await client.get_or_load("k", loader=loader, ttl_s=300.0)

    assert value == "cached"
    assert calls == 0


async def test_soft_stale_serves_the_old_value_and_refreshes_in_the_background() -> None:
    clock = FakeClock()
    redis = FakeRedis()
    client = _client(redis, clock=clock)
    await client.set_if_newer("k", version=1, value="old", ttl_s=10.0)  # soft_exp = now + 8s

    clock.advance(9.0)  # past soft_exp, still within the hard 10s TTL
    refreshed = asyncio.Event()

    async def loader() -> tuple[int, str]:
        refreshed.set()
        return 2, "new"

    value = await client.get_or_load("k", loader=loader, ttl_s=10.0)
    assert value == "old"  # served immediately, not the refreshed value

    await asyncio.wait_for(refreshed.wait(), timeout=1.0)
    await asyncio.sleep(0)  # let the background task finish writing the cache
    updated = await client.get("k")
    assert updated is not None
    assert updated.value == "new"


async def test_set_if_newer_rejects_an_older_version() -> None:
    redis = FakeRedis()
    client = _client(redis)
    await client.set_if_newer("k", version=5, value="newer", ttl_s=300.0)

    await client.set_if_newer("k", version=3, value="stale write", ttl_s=300.0)

    hit = await client.get("k")
    assert hit is not None
    assert hit.value == "newer"


async def test_invalidate_removes_the_cached_value() -> None:
    redis = FakeRedis()
    client = _client(redis)
    await client.set_if_newer("k", version=1, value="cached", ttl_s=300.0)

    await client.invalidate("k")

    assert await client.get("k") is None


async def test_redis_failure_bypasses_the_cache_rather_than_erroring() -> None:
    redis = FakeRedis()
    redis.fail = True
    client = _client(redis)
    calls = 0

    async def loader() -> tuple[int, str]:
        nonlocal calls
        calls += 1
        return 1, "from-db"

    value = await client.get_or_load("k", loader=loader, ttl_s=300.0)

    assert value == "from-db"
    assert calls == 1


async def test_a_losing_peer_waits_then_sees_the_winners_value() -> None:
    redis = FakeRedis()
    client = _client(redis)
    winner_started = asyncio.Event()
    release_winner = asyncio.Event()

    async def slow_loader() -> tuple[int, str]:
        winner_started.set()
        await release_winner.wait()
        return 1, "winner-value"

    async def fast_loader() -> tuple[int, str]:
        raise AssertionError(
            "the losing peer must not load itself -- it should see the winner's write"
        )

    async def winner() -> str:
        return await client.get_or_load("k", loader=slow_loader, ttl_s=300.0)

    # A second CacheClient simulates a second pod/process -- same Redis,
    # independent single-flight state, so this exercises the cross-pod
    # lock (§6.3.3), not the per-pod single-flight (§6.3.4).
    peer = _client(redis)

    winner_task = asyncio.create_task(winner())
    await winner_started.wait()
    peer_task = asyncio.create_task(peer.get_or_load("k", loader=fast_loader, ttl_s=300.0))

    await asyncio.sleep(0.02)  # let the peer's own lock attempt fail and start polling
    release_winner.set()

    assert await winner_task == "winner-value"
    assert await peer_task == "winner-value"


async def test_per_pod_single_flight_loads_only_once_for_concurrent_callers() -> None:
    redis = FakeRedis()
    client = _client(redis)
    calls = 0
    release = asyncio.Event()

    async def loader() -> tuple[int, str]:
        nonlocal calls
        calls += 1
        await release.wait()
        return 1, "value"

    task1 = asyncio.create_task(client.get_or_load("k", loader=loader, ttl_s=300.0))
    task2 = asyncio.create_task(client.get_or_load("k", loader=loader, ttl_s=300.0))
    await asyncio.sleep(0.01)
    release.set()

    assert await task1 == "value"
    assert await task2 == "value"
    assert calls == 1


async def test_bulkhead_full_propagates_rather_than_being_swallowed() -> None:
    from sf_common.bulkhead import BulkheadFullError

    redis = FakeRedis()
    client = CacheClient(redis, name="saturated-cache", load_concurrency=0)  # type: ignore[arg-type]

    async def loader() -> tuple[int, str]:
        return 1, "unreachable"

    with pytest.raises(BulkheadFullError):
        await client.get_or_load("k", loader=loader, ttl_s=300.0)


async def test_cache_breaker_opens_after_repeated_redis_failures() -> None:
    redis = FakeRedis()
    redis.fail = True
    breaker = CircuitBreaker(
        service="x", dependency="redis-cache", slow_call_duration_s=0.5, min_calls=1
    )
    client = CacheClient(redis, name="x", breaker=breaker)  # type: ignore[arg-type]

    async def loader() -> tuple[int, str]:
        return 1, "from-db"

    await client.get_or_load("k", loader=loader, ttl_s=300.0)

    from sf_common.circuit_breaker import CircuitState

    assert breaker.state is CircuitState.OPEN
