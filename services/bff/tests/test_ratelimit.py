"""GCRA rate limiting (LLD-09 §7). No live redis-cache needed -- ``FakeRedis``
below re-implements ``GCRA_LUA``'s own algorithm in Python (same "fake the
script's behavior, not a real Lua interpreter" pattern as bff's own
``test_proxy.py``), so a bug in the Python/Lua boundary (the millisecond
integer-truncation trick) would show up here as clearly as a bug in the
algorithm itself.
"""

import math

import pytest
from app.ratelimit import (
    AUTH,
    CHECKOUT,
    MUTATION,
    PUBLIC,
    SEARCH,
    RateLimitClass,
    RateLimiter,
    _InProcessGcra,
    classify,
)
from redis.exceptions import RedisError


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, float] = {}
        self.fail = False

    async def eval(self, script: str, _numkeys: int, key: str, *args: object) -> list[int]:
        if self.fail:
            raise RedisError("redis-cache unreachable")
        assert script  # the real script text; this fake doesn't parse it
        burst, rate, period, cost, now = (float(a) for a in args)
        emission_interval = period / rate
        increment = emission_interval * cost
        burst_offset = emission_interval * burst

        tat = self.store.get(key, now)
        tat = max(tat, now)
        new_tat = tat + increment
        allow_at = new_tat - burst_offset
        diff = now - allow_at

        if diff < 0:
            reset_after = tat - now
            retry_after = -diff
            return [0, 0, math.ceil(retry_after * 1000), math.ceil(reset_after * 1000)]

        remaining = int(diff / emission_interval)
        reset_after = new_tat - now
        if reset_after > 0:
            self.store[key] = new_tat
        return [1, remaining, -1, math.ceil(reset_after * 1000)]


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


async def test_allows_requests_within_the_limit() -> None:
    redis = FakeRedis()
    limiter = RateLimiter(redis, clock=FakeClock())

    decision = await limiter.check(AUTH, "ip:1.2.3.4")

    assert decision.allowed is True


async def test_rejects_once_burst_and_rate_are_exhausted() -> None:
    redis = FakeRedis()
    clock = FakeClock()
    limiter = RateLimiter(redis, clock=clock)
    tiny = RateLimitClass("tiny", limit=1, period_s=60.0, burst=1)

    first = await limiter.check(tiny, "ip:1.2.3.4")
    second = await limiter.check(tiny, "ip:1.2.3.4")

    assert first.allowed is True
    assert second.allowed is False
    assert second.retry_after_s > 0


async def test_retry_after_elapses_then_allows_again() -> None:
    redis = FakeRedis()
    clock = FakeClock()
    limiter = RateLimiter(redis, clock=clock)
    tiny = RateLimitClass("tiny", limit=1, period_s=60.0, burst=1)

    await limiter.check(tiny, "ip:1.2.3.4")
    rejected = await limiter.check(tiny, "ip:1.2.3.4")
    assert rejected.allowed is False

    clock.advance(rejected.retry_after_s + 0.01)
    allowed_again = await limiter.check(tiny, "ip:1.2.3.4")

    assert allowed_again.allowed is True


async def test_different_keys_have_independent_buckets() -> None:
    redis = FakeRedis()
    limiter = RateLimiter(redis, clock=FakeClock())
    tiny = RateLimitClass("tiny", limit=1, period_s=60.0, burst=1)

    await limiter.check(tiny, "ip:1.2.3.4")
    other = await limiter.check(tiny, "ip:5.6.7.8")

    assert other.allowed is True


async def test_falls_back_to_in_process_when_redis_is_unreachable() -> None:
    redis = FakeRedis()
    redis.fail = True
    limiter = RateLimiter(redis, clock=FakeClock())

    decision = await limiter.check(CHECKOUT, "user:u1")

    assert decision.allowed is True  # the fallback still enforces a (stricter) limit


async def test_fallback_is_stricter_for_auth_and_enumeration() -> None:
    redis = FakeRedis()
    redis.fail = True
    limiter = RateLimiter(redis, replica_count=2, clock=FakeClock())

    # AUTH has burst=20; with replica_count=2 and the 2x-stricter rule for
    # "auth", the fallback's effective burst is 20 // (2*2) = 5.
    for _ in range(5):
        decision = await limiter.check(AUTH, "ip:1.2.3.4")
        assert decision.allowed is True
    rejected = await limiter.check(AUTH, "ip:1.2.3.4")
    assert rejected.allowed is False


def test_in_process_gcra_matches_the_same_algorithm_directly() -> None:
    """``burst`` is the total number of requests a fresh bucket admits at
    once (not "the rate's own one request, plus burst more")."""
    fallback = _InProcessGcra()
    rate_class = RateLimitClass("x", limit=2, period_s=10.0, burst=2)

    first = fallback.check(rate_class, "k", cost=1, now=0.0)
    second = fallback.check(rate_class, "k", cost=1, now=0.0)
    third = fallback.check(rate_class, "k", cost=1, now=0.0)

    assert first.allowed is True
    assert second.allowed is True
    assert third.allowed is False


@pytest.mark.parametrize("rate_class", [AUTH, CHECKOUT])
async def test_decisions_are_reported_with_the_class_name(rate_class: RateLimitClass) -> None:
    """Loosely confirms the metric call doesn't blow up for every real
    class this module exports -- the OTel SDK itself is a no-op without
    ``init_telemetry`` (sf_common's own documented behavior), so this
    can't assert on exported values, only that nothing raises."""
    redis = FakeRedis()
    limiter = RateLimiter(redis, clock=FakeClock())

    await limiter.check(rate_class, "k")


def test_classify_puts_search_calls_in_the_search_class() -> None:
    assert classify("search", "v1/stores/org-1/search", "GET") is SEARCH


def test_classify_puts_checkout_posts_in_the_checkout_class() -> None:
    assert classify("orders", "v1/stores/org-1/checkouts", "POST") is CHECKOUT


def test_classify_puts_order_cancel_in_the_checkout_class() -> None:
    assert classify("orders", "v1/me/orders/order-1:cancel", "POST") is CHECKOUT


def test_classify_puts_other_order_gets_in_public_not_checkout() -> None:
    assert classify("orders", "v1/me/orders/order-1", "GET") is PUBLIC


def test_classify_puts_authenticated_reads_in_public() -> None:
    assert classify("catalog", "v1/stores/org-1/products/widget", "GET") is PUBLIC


def test_classify_puts_other_writes_in_mutation() -> None:
    assert classify("cart", "v1/stores/org-1/cart/items", "POST") is MUTATION
