"""Per-dependency circuit breakers (LLD-13 §2.3).

One breaker per (service, dependency) pair -- "service" is this process's
own name, "dependency" the thing it's calling, matching the
``circuit_breaker_state`` metric's own labels. Half-open admits probes one
at a time (not concurrently): a flood of concurrent requests the instant a
breaker opens should not all land as unsynchronized "probes" that could
each independently flip the breaker back open.
"""

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from enum import IntEnum
from typing import TypeVar

from opentelemetry import metrics
from opentelemetry.metrics import CallbackOptions, Observation

T = TypeVar("T")

_meter = metrics.get_meter("sf_common")
_states: dict[tuple[str, str], "CircuitState"] = {}


class CircuitState(IntEnum):
    CLOSED = 0
    HALF_OPEN = 1
    OPEN = 2


class CircuitOpenError(Exception):
    """Raised instead of making a call while the breaker is open (503
    `dependency-unavailable`, or the documented degradation)."""


def _observe_state(_options: CallbackOptions) -> Iterable[Observation]:
    for (service, dependency), state in list(_states.items()):
        yield Observation(int(state), {"service": service, "dependency": dependency})


_meter.create_observable_gauge(
    "circuit_breaker_state",
    callbacks=[_observe_state],
    unit="{state}",
    description="0 closed, 1 half-open, 2 open (LLD-13 §2.3)",
)


class CircuitBreaker:
    def __init__(
        self,
        *,
        service: str,
        dependency: str,
        slow_call_duration_s: float,
        window_s: float = 10.0,
        min_calls: int = 10,
        failure_rate_threshold: float = 0.5,
        slow_call_rate_threshold: float = 0.8,
        open_duration_s: float = 15.0,
        half_open_probes: int = 3,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._key = (service, dependency)
        self._slow_s = slow_call_duration_s
        self._window_s = window_s
        self._min_calls = min_calls
        self._failure_rate_threshold = failure_rate_threshold
        self._slow_call_rate_threshold = slow_call_rate_threshold
        self._open_duration_s = open_duration_s
        self._half_open_probes = half_open_probes
        self._clock = clock

        self._state = CircuitState.CLOSED
        self._open_until = 0.0
        self._calls: deque[tuple[float, bool, bool]] = deque()  # (ts, failed, slow)
        self._half_open_lock = asyncio.Lock()
        self._half_open_passed = 0
        self._lock = asyncio.Lock()
        _states[self._key] = self._state

    @property
    def state(self) -> CircuitState:
        return self._state

    def _set_state(self, state: CircuitState) -> None:
        self._state = state
        _states[self._key] = state

    def _prune(self) -> None:
        cutoff = self._clock() - self._window_s
        while self._calls and self._calls[0][0] < cutoff:
            self._calls.popleft()

    async def call(self, fn: Callable[[], Awaitable[T]]) -> T:
        async with self._lock:
            now = self._clock()
            if self._state is CircuitState.OPEN:
                if now < self._open_until:
                    raise CircuitOpenError
                self._set_state(CircuitState.HALF_OPEN)
                self._half_open_passed = 0

        if self._state is CircuitState.HALF_OPEN:
            async with self._half_open_lock:
                return await self._call_half_open(fn)
        return await self._call_closed(fn)

    async def _call_half_open(self, fn: Callable[[], Awaitable[T]]) -> T:
        try:
            result, _ = await self._timed(fn)
        except Exception:
            async with self._lock:
                self._set_state(CircuitState.OPEN)
                self._open_until = self._clock() + self._open_duration_s
            raise
        async with self._lock:
            self._half_open_passed += 1
            if self._half_open_passed >= self._half_open_probes:
                self._set_state(CircuitState.CLOSED)
                self._calls.clear()
        return result

    async def _call_closed(self, fn: Callable[[], Awaitable[T]]) -> T:
        try:
            result, slow = await self._timed(fn)
        except Exception:
            self._record(failed=True, slow=False)
            raise
        self._record(failed=False, slow=slow)
        return result

    async def _timed(self, fn: Callable[[], Awaitable[T]]) -> tuple[T, bool]:
        started = self._clock()
        result = await fn()
        slow = (self._clock() - started) > self._slow_s
        return result, slow

    def _record(self, *, failed: bool, slow: bool) -> None:
        self._calls.append((self._clock(), failed, slow))
        self._prune()
        total = len(self._calls)
        if total < self._min_calls:
            return
        failures = sum(1 for _, f, _ in self._calls if f)
        slows = sum(1 for _, _, s in self._calls if s)
        if (
            failures / total >= self._failure_rate_threshold
            or slows / total >= self._slow_call_rate_threshold
        ):
            self._set_state(CircuitState.OPEN)
            self._open_until = self._clock() + self._open_duration_s
            self._calls.clear()
