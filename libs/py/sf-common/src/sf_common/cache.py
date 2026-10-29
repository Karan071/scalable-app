"""Cache-aside with stampede protection (LLD-09 §6).

One ``CacheClient`` per cache (§6.1's table: "Owner (does cache-aside)" --
catalog-svc, orgs-svc, inventory-svc each construct their own, one per
named cache, pointed at redis-cache). Reuses this package's own
``Bulkhead`` (the §6.4 "per-pod semaphore for cache-miss loads") and
``CircuitBreaker`` (§6.4's "circuit breaker on the cache client") rather
than reinventing either.
"""

import asyncio
import json
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

from opentelemetry import metrics
from redis.asyncio import Redis

from .bulkhead import Bulkhead
from .circuit_breaker import CircuitBreaker, CircuitOpenError

T = TypeVar("T")

_meter = metrics.get_meter("sf_common")
cache_requests_total = _meter.create_counter(
    "cache_requests_total",
    unit="{request}",
    description="Cache-aside reads by cache and result: hit, stale_served, "
    "miss, stampede_wait, bypass (LLD-09 §6.5)",
)
cache_load_duration_seconds = _meter.create_histogram(
    "cache_load_duration_seconds", unit="s", description="Loader duration on a cache miss"
)
cache_stampede_waits_total = _meter.create_counter(
    "cache_stampede_waits_total",
    unit="{request}",
    description="Requests that waited on another in-flight load rather than loading themselves",
)

# KEYS[1] = cache key. ARGV = new_version, payload (JSON), ttl_s. "newer or
# absent" (§6.2): never lets a slow loader's stale result clobber a value
# a faster write (or a delete-then-repopulate) already landed.
SET_IF_NEWER_LUA = """
local existing = redis.call("GET", KEYS[1])
if existing then
  local ok, decoded = pcall(cjson.decode, existing)
  if ok and decoded["v"] and tonumber(decoded["v"]) > tonumber(ARGV[1]) then
    return 0
  end
end
redis.call("SET", KEYS[1], ARGV[2], "EX", math.ceil(tonumber(ARGV[3])))
return 1
"""


@dataclass(frozen=True)
class CacheHit:
    version: int
    value: object
    soft_exp: float


class CacheClient:
    def __init__(
        self,
        redis: Redis,
        *,
        name: str,
        load_concurrency: int = 20,
        refresh_lock_ttl_ms: int = 2000,
        stampede_wait_ms: int = 150,
        stampede_poll_interval_ms: int = 10,
        breaker: CircuitBreaker | None = None,
        clock: Callable[[], float] = time.time,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self._redis = redis
        self._name = name
        self._load_semaphore = Bulkhead(load_concurrency)
        self._refresh_lock_ttl_ms = refresh_lock_ttl_ms
        self._stampede_wait_ms = stampede_wait_ms
        self._stampede_poll_interval_ms = stampede_poll_interval_ms
        # §6.4: "open after 5 failures in 10s, half-open probe after 30s".
        self._breaker = breaker or CircuitBreaker(
            service=name,
            dependency="redis-cache",
            slow_call_duration_s=0.5,
            window_s=10.0,
            min_calls=5,
            failure_rate_threshold=1.0,
            open_duration_s=30.0,
            half_open_probes=1,
        )
        self._clock = clock
        self._rng = rng
        # Per-pod single-flight (§6.3.4): one in-flight load per key per
        # process. Popped once the holder is done, so this never grows
        # unbounded with the set of keys ever seen.
        self._single_flight: dict[str, asyncio.Lock] = {}

    async def get(self, key: str) -> CacheHit | None:
        raw = await self._redis.get(key)
        if raw is None:
            return None
        try:
            decoded = json.loads(raw)
            return CacheHit(
                version=int(decoded["v"]), value=decoded["val"], soft_exp=float(decoded["soft"])
            )
        except (ValueError, KeyError, TypeError):
            return None

    async def set_if_newer(
        self,
        key: str,
        *,
        version: int,
        value: object,
        ttl_s: float,
        jitter: float = 0.1,
        soft_ttl_ratio: float = 0.8,
    ) -> None:
        jittered_ttl = ttl_s * (1 + self._rng() * 2 * jitter - jitter)
        soft_exp = self._clock() + jittered_ttl * soft_ttl_ratio
        payload = json.dumps({"v": version, "val": value, "soft": soft_exp})
        await self._redis.eval(SET_IF_NEWER_LUA, 1, key, version, payload, jittered_ttl)

    async def invalidate(self, key: str) -> None:
        """One half of the §6.1 "double delete" -- the other half is the
        owning service's own event consumer calling this a second time
        once ``*.updated`` actually arrives."""
        await self._redis.delete(key)

    async def get_or_load(
        self,
        key: str,
        *,
        loader: Callable[[], Awaitable[tuple[int, T]]],
        ttl_s: float,
        jitter: float = 0.1,
        soft_ttl_ratio: float = 0.8,
    ) -> T:
        """``loader`` returns ``(version, value)`` on an actual DB load --
        version is whatever the owning service already uses to detect a
        stale write (e.g. a row's own optimistic-lock version column).

        Raises ``sf_common.BulkheadFullError`` when the per-pod load
        semaphore (§6.4) is saturated -- the caller's own route maps that
        to 503 ``Retry-After: 1``, same as every other bulkhead in this
        codebase (LLD-13 §2.4); this module doesn't assume HTTP.
        """
        cached, bypass = await self._safe_get(key)
        now = self._clock()

        if cached is not None:
            if now < cached.soft_exp:
                cache_requests_total.add(1, {"cache": self._name, "result": "hit"})
                return cached.value  # type: ignore[return-value]
            # Soft-stale (§6.3.2): serve it, and if we win the refresh
            # lock, refresh in the background rather than making this
            # request wait on a DB load it doesn't need to wait on.
            if await self._try_lock(key):
                asyncio.create_task(  # noqa: RUF006 -- fire-and-forget refresh by design
                    self._refresh(key, loader, ttl_s, jitter, soft_ttl_ratio)
                )
            cache_requests_total.add(1, {"cache": self._name, "result": "stale_served"})
            return cached.value  # type: ignore[return-value]

        if bypass:
            cache_requests_total.add(1, {"cache": self._name, "result": "bypass"})
            _, value = await self._load(loader)
            return value

        lock = self._single_flight.setdefault(key, asyncio.Lock())
        async with lock:
            try:
                return await self._load_on_hard_miss(key, loader, ttl_s, jitter, soft_ttl_ratio)
            finally:
                self._single_flight.pop(key, None)

    async def _load_on_hard_miss(
        self,
        key: str,
        loader: Callable[[], Awaitable[tuple[int, T]]],
        ttl_s: float,
        jitter: float,
        soft_ttl_ratio: float,
    ) -> T:
        # Another task in this process may have already populated the key
        # while this one waited for the single-flight lock.
        recheck, bypass = await self._safe_get(key)
        if recheck is not None and not bypass and self._clock() < recheck.soft_exp:
            cache_requests_total.add(1, {"cache": self._name, "result": "hit"})
            return recheck.value  # type: ignore[return-value]

        if await self._try_lock(key):
            try:
                version, value = await self._load(loader)
                await self.set_if_newer(
                    key,
                    version=version,
                    value=value,
                    ttl_s=ttl_s,
                    jitter=jitter,
                    soft_ttl_ratio=soft_ttl_ratio,
                )
            finally:
                await self._unlock(key)
            cache_requests_total.add(1, {"cache": self._name, "result": "miss"})
            return value

        # §6.3.3: a cross-pod peer is loading -- wait briefly rather than
        # stampeding the DB alongside it.
        waited = await self._wait_for_peer(key)
        if waited is not None:
            cache_stampede_waits_total.add(1, {"cache": self._name})
            return waited.value  # type: ignore[return-value]

        cache_requests_total.add(1, {"cache": self._name, "result": "miss"})
        _, value = await self._load(loader)
        return value

    async def _safe_get(self, key: str) -> tuple[CacheHit | None, bool]:
        try:
            hit = await self._breaker.call(lambda: self.get(key))
        except CircuitOpenError:
            return None, True
        except Exception:
            return None, True
        return hit, False

    async def _try_lock(self, key: str) -> bool:
        try:
            return bool(
                await self._redis.set(f"lock:{key}", "1", nx=True, px=self._refresh_lock_ttl_ms)
            )
        except Exception:
            return False

    async def _unlock(self, key: str) -> None:
        try:
            await self._redis.delete(f"lock:{key}")
        except Exception:
            pass

    async def _wait_for_peer(self, key: str) -> CacheHit | None:
        deadline = self._clock() + self._stampede_wait_ms / 1000.0
        while self._clock() < deadline:
            await asyncio.sleep(self._stampede_poll_interval_ms / 1000.0)
            hit, bypass = await self._safe_get(key)
            if hit is not None and not bypass:
                return hit
        return None

    async def _load(self, loader: Callable[[], Awaitable[tuple[int, T]]]) -> tuple[int, T]:
        async with self._load_semaphore.guard():
            started = self._clock()
            result = await loader()
            cache_load_duration_seconds.record(self._clock() - started, {"cache": self._name})
            return result

    async def _refresh(
        self,
        key: str,
        loader: Callable[[], Awaitable[tuple[int, T]]],
        ttl_s: float,
        jitter: float,
        soft_ttl_ratio: float,
    ) -> None:
        try:
            version, value = await self._load(loader)
            await self.set_if_newer(
                key,
                version=version,
                value=value,
                ttl_s=ttl_s,
                jitter=jitter,
                soft_ttl_ratio=soft_ttl_ratio,
            )
        finally:
            await self._unlock(key)
