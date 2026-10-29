"""Per-dependency concurrency bulkheads (LLD-13 §2.4).

Non-blocking: a caller that can't get a slot fails fast instead of
queueing, so a backlog against one dependency can't starve callers waiting
on a semaphore that will never free up in time. ``asyncio.Semaphore`` has
no non-blocking acquire, hence this instead of one.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class BulkheadFullError(Exception):
    """Raised instead of queueing (503 `dependency-unavailable` or the
    documented degradation -- the caller's job, not this class's)."""


class Bulkhead:
    def __init__(self, max_concurrency: int) -> None:
        self._max = max_concurrency
        self._current = 0
        self._lock = asyncio.Lock()

    @property
    def in_flight(self) -> int:
        return self._current

    async def try_acquire(self) -> bool:
        async with self._lock:
            if self._current >= self._max:
                return False
            self._current += 1
            return True

    async def release(self) -> None:
        async with self._lock:
            self._current -= 1

    @asynccontextmanager
    async def guard(self) -> AsyncIterator[None]:
        if not await self.try_acquire():
            raise BulkheadFullError
        try:
            yield
        finally:
            await self.release()
