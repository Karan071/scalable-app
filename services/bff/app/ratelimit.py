"""GCRA (generic cell rate algorithm) rate limiting (LLD-09 §7).

One Lua script, atomic in Redis, so every BFF replica shares the same
limit state (§7.1: "the state is shared in Redis" -- a 429 looks
identical regardless of which replica answered). Falls back to an
in-process GCRA, deliberately stricter (limit divided by replica count,
§7.2) when redis-cache is unreachable, rather than either failing open (no
limit at all) or failing closed (blocking everything).

The Lua script is the same algorithm go-redis/redis_rate uses (a widely
deployed reference implementation of GCRA over Redis), adapted here with
millisecond-scaled integer outputs: Redis converts a Lua number reply to
an integer by truncating it, so returning fractional seconds directly
would silently lose precision on every call.
"""

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, replace

from fastapi import HTTPException
from opentelemetry import metrics
from redis.asyncio import Redis
from redis.exceptions import RedisError

_meter = metrics.get_meter("bff")
ratelimit_decisions_total = _meter.create_counter(
    "ratelimit_decisions_total",
    unit="{request}",
    description="Rate-limit checks by class and result (LLD-09 §7.2)",
)
ratelimit_fallback_active = _meter.create_up_down_counter(
    "ratelimit_fallback_active",
    unit="{instance}",
    description="1 while this process is using the in-process GCRA fallback "
    "(redis-cache unreachable), 0 otherwise",
)

# KEYS[1] = bucket key. ARGV = burst, rate (limit), period_s, cost, now_s.
# Returns {allowed(0/1), remaining, retry_after_ms (-1 if allowed), reset_after_ms}.
GCRA_LUA = """
local key = KEYS[1]
local burst = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local period = tonumber(ARGV[3])
local cost = tonumber(ARGV[4])
local now = tonumber(ARGV[5])

local emission_interval = period / rate
local increment = emission_interval * cost
local burst_offset = emission_interval * burst

local tat = tonumber(redis.call("GET", key))
if tat == nil then
  tat = now
end
tat = math.max(tat, now)

local new_tat = tat + increment
local allow_at = new_tat - burst_offset
local diff = now - allow_at

if diff < 0 then
  local reset_after = tat - now
  local retry_after = -diff
  return {0, 0, math.ceil(retry_after * 1000), math.ceil(reset_after * 1000)}
end

local remaining = math.floor(diff / emission_interval)
local reset_after = new_tat - now
if reset_after > 0 then
  redis.call("SET", key, new_tat, "EX", math.ceil(reset_after))
end
return {1, remaining, -1, math.ceil(reset_after * 1000)}
"""


@dataclass(frozen=True)
class RateLimitClass:
    name: str
    limit: int
    period_s: float
    burst: int


# LLD-09 §7.2's initial values (calibrated in M4's own load tests, not
# done yet -- these are starting points). `sse` (a concurrency counter,
# not a rate) isn't here: it needs E4.7's SSE endpoint to exist first.
# `enumeration`'s own "and 50/hour" and `admin_expensive`'s "100/hour per
# org" are second, longer-window limits this single-class-per-check GCRA
# doesn't model -- only the per-minute/per-user figure is enforced here,
# a documented simplification, not an oversight.
#
# §7.2 only states a `burst` value for `public`/`search`; every other
# class defaults to `burst == limit` (a full period's worth admitted
# instantly, then steady refill) -- a reasonable default, not an LLD
# number, since the LLD doesn't give one.
AUTH = RateLimitClass("auth", limit=20, period_s=60.0, burst=20)
PUBLIC = RateLimitClass("public", limit=300, period_s=60.0, burst=60)
SEARCH = RateLimitClass("search", limit=60, period_s=60.0, burst=20)
CHECKOUT = RateLimitClass("checkout", limit=10, period_s=60.0, burst=10)
MUTATION = RateLimitClass("mutation", limit=120, period_s=60.0, burst=120)
ENUMERATION = RateLimitClass("enumeration", limit=10, period_s=60.0, burst=10)
ADMIN_EXPENSIVE = RateLimitClass("admin_expensive", limit=10, period_s=60.0, burst=10)

#: §7.2: the fallback errs stricter for these two.
_STRICTER_FALLBACK_CLASSES = {"auth", "enumeration"}


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    remaining: int
    retry_after_s: float  # -1.0 when allowed
    reset_after_s: float


class _InProcessGcra:
    """Single-process GCRA -- the §7.2 Redis-outage fallback. Not shared
    across replicas (that's exactly why the real limit is divided down
    before reaching here: each replica independently enforcing
    ``limit / replica_count`` approximates one shared limit of ``limit``,
    rather than each one allowing the full ``limit`` on its own)."""

    def __init__(self) -> None:
        self._tat: dict[str, float] = {}

    def check(
        self, rate_class: RateLimitClass, key: str, *, cost: int, now: float
    ) -> RateLimitDecision:
        emission_interval = rate_class.period_s / rate_class.limit
        increment = emission_interval * cost
        burst_offset = emission_interval * rate_class.burst

        tat = max(self._tat.get(key, now), now)
        new_tat = tat + increment
        allow_at = new_tat - burst_offset
        diff = now - allow_at

        if diff < 0:
            return RateLimitDecision(False, 0, -diff, tat - now)

        self._tat[key] = new_tat
        remaining = int(diff / emission_interval)
        return RateLimitDecision(True, remaining, -1.0, new_tat - now)


class RateLimiter:
    def __init__(
        self, redis: Redis, *, replica_count: int = 1, clock: Callable[[], float] = time.time
    ) -> None:
        self._redis = redis
        self._replica_count = max(replica_count, 1)
        self._clock = clock
        self._fallback = _InProcessGcra()
        self._fallback_active = False

    async def check(
        self, rate_class: RateLimitClass, key: str, *, cost: int = 1
    ) -> RateLimitDecision:
        now = self._clock()
        bucket_key = f"ratelimit:{rate_class.name}:{key}"
        try:
            raw = await self._redis.eval(
                GCRA_LUA,
                1,
                bucket_key,
                rate_class.burst,
                rate_class.limit,
                rate_class.period_s,
                cost,
                now,
            )
        except RedisError:
            self._set_fallback_active(True)
            decision = self._fallback_check(rate_class, key, cost=cost, now=now)
        else:
            self._set_fallback_active(False)
            allowed, remaining, retry_after_ms, reset_after_ms = raw
            decision = RateLimitDecision(
                allowed=bool(allowed),
                remaining=int(remaining),
                retry_after_s=-1.0 if int(retry_after_ms) < 0 else int(retry_after_ms) / 1000.0,
                reset_after_s=int(reset_after_ms) / 1000.0,
            )

        ratelimit_decisions_total.add(
            1, {"class": rate_class.name, "result": "allowed" if decision.allowed else "rejected"}
        )
        return decision

    def _fallback_check(
        self, rate_class: RateLimitClass, key: str, *, cost: int, now: float
    ) -> RateLimitDecision:
        divisor = self._replica_count * (2 if rate_class.name in _STRICTER_FALLBACK_CLASSES else 1)
        stricter = replace(
            rate_class,
            limit=max(1, rate_class.limit // divisor),
            burst=max(1, rate_class.burst // divisor),
        )
        return self._fallback.check(stricter, key, cost=cost, now=now)

    def _set_fallback_active(self, active: bool) -> None:
        if active == self._fallback_active:
            return
        ratelimit_fallback_active.add(1 if active else -1)
        self._fallback_active = active


def classify(service: str, downstream_path: str, method: str) -> RateLimitClass:
    """Approximates LLD-09 §7.2's classes from (service, path, method)
    rather than per-route metadata -- the more faithful approach (the
    same per-route ``x-sf-authz`` tagging authz-svc's own routes already
    carry) is future work, not done here. ``enumeration``/
    ``admin_expensive`` aren't distinguished from ``public``/``mutation``
    by this: their own routes (handle availability, invitation accept,
    exports, reindex triggers) aren't pattern-matched specifically yet."""
    if service == "search":
        return SEARCH
    if (
        service == "orders"
        and method == "POST"
        and (downstream_path.endswith("checkouts") or downstream_path.endswith(":cancel"))
    ):
        return CHECKOUT
    if method == "GET":
        return PUBLIC
    return MUTATION


async def enforce(
    limiter: RateLimiter, rate_class: RateLimitClass, key: str, *, cost: int = 1
) -> None:
    """Raises the §7.2 429 if over the limit; returns silently otherwise.
    ``sf_common.install_problem_details``'s generic ``HTTPException``
    handler turns ``detail`` into the problem's ``title`` and forwards
    ``headers`` -- this doesn't build the response body itself."""
    decision = await limiter.check(rate_class, key, cost=cost)
    if decision.allowed:
        return
    policy = f'"{rate_class.name}";q={rate_class.limit};w={int(rate_class.period_s)}'
    reset = max(0, math.ceil(decision.reset_after_s))
    raise HTTPException(
        status_code=429,
        detail="rate-limited",
        headers={
            "retry-after": str(max(1, math.ceil(decision.retry_after_s))),
            "ratelimit-policy": policy,
            "ratelimit": f"limit={rate_class.limit}, remaining=0, reset={reset}",
        },
    )
