"""HTTP call retries with full jitter and a retry budget (LLD-13 §2.2).

For outbound service-to-service calls (``sf_db.retry_on_transient`` is the
separate DB-operation equivalent: deadlocks/serialization failures, not
HTTP). ``idempotent`` is the caller's own determination -- GET/HEAD/PUT/
DELETE, or POST with an Idempotency-Key or to an endpoint documented as
idempotent by business key (LLD-13 §2.2's eligibility table) -- this
module trusts it rather than inspecting the request itself, since only the
caller knows which of those is true for a given call.
"""

import asyncio
import random
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Protocol, TypeVar

T = TypeVar("T")

_RETRYABLE_STATUS = {429, 502, 503, 504}


class _Response(Protocol):
    status_code: int
    headers: object


class RetryBudget:
    """Token bucket, counted as ratios rather than fixed tokens: a retry is
    allowed only while retries stay at or under ``retry_ratio`` of requests
    made in the trailing ``window_s`` (LLD-13 §2.2's "retries ≤ 10% of
    requests over 10s"). Shared across calls to the same dependency --
    construct one per dependency, not one per call."""

    def __init__(
        self,
        *,
        window_s: float = 10.0,
        retry_ratio: float = 0.1,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._window_s = window_s
        self._ratio = retry_ratio
        self._clock = clock
        self._requests: deque[float] = deque()
        self._retries: deque[float] = deque()

    def record_request(self) -> None:
        self._requests.append(self._clock())
        self._prune()

    def try_consume_retry(self) -> bool:
        self._prune()
        if len(self._retries) >= self._ratio * len(self._requests):
            return False
        self._retries.append(self._clock())
        return True

    def _prune(self) -> None:
        cutoff = self._clock() - self._window_s
        for dq in (self._requests, self._retries):
            while dq and dq[0] < cutoff:
                dq.popleft()


def full_jitter_backoff_s(
    attempt: int,
    *,
    base_ms: float = 100.0,
    cap_ms: float = 1000.0,
    rng: Callable[[], float] = random.random,
) -> float:
    """``sleep = random(0, min(cap, base * 2^attempt))``, in seconds."""
    return rng() * min(cap_ms, base_ms * (2**attempt)) / 1000.0


def _retry_after_s(response: _Response) -> float | None:
    value = getattr(response.headers, "get", lambda *_: None)("retry-after")
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


async def send_with_retries(  # noqa: UP047 -- TypeVar style, matches sf_db.retry_on_transient
    send: Callable[[], Awaitable[T]],
    *,
    idempotent: bool,
    budget: RetryBudget,
    retryable_errors: tuple[type[Exception], ...] = (),
    max_attempts: int = 3,
    base_ms: float = 100.0,
    cap_ms: float = 1000.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """Retries ``send()`` up to ``max_attempts`` total while ``idempotent``,
    the retry budget allows it, and the deadline (the caller's own
    responsibility to check before calling ``send`` again -- this function
    has no deadline of its own) hasn't passed. A response is retried only
    if it has a ``status_code`` in {429, 502, 503, 504}; any other outcome
    (including a non-retryable exception) returns/raises immediately."""
    budget.record_request()
    attempt = 0
    while True:
        try:
            result = await send()
        except retryable_errors:
            if not idempotent or attempt + 1 >= max_attempts or not budget.try_consume_retry():
                raise
            retry_after_s = None
        else:
            status = getattr(result, "status_code", None)
            if status not in _RETRYABLE_STATUS:
                return result
            if not idempotent or attempt + 1 >= max_attempts or not budget.try_consume_retry():
                return result
            retry_after_s = _retry_after_s(result) if status == 429 else None  # type: ignore[arg-type]

        backoff_s = full_jitter_backoff_s(attempt, base_ms=base_ms, cap_ms=cap_ms)
        if retry_after_s is not None:
            backoff_s = max(backoff_s, retry_after_s)
        await sleep(backoff_s)
        attempt += 1
