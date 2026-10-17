"""Deadlock/serialization retry for multi-row writes (LLD-09 §3)."""

import asyncio
import functools
import random
from collections.abc import Awaitable, Callable
from typing import ParamSpec, TypeVar

from sqlalchemy.exc import DBAPIError

# Postgres SQLSTATEs: serialization_failure, deadlock_detected.
_TRANSIENT_SQLSTATES = frozenset({"40001", "40P01"})

P = ParamSpec("P")
T = TypeVar("T")


def _sqlstate(exc: DBAPIError) -> str | None:
    return getattr(getattr(exc, "orig", None), "sqlstate", None)


def retry_on_transient(
    attempts: int = 3, base_delay_s: float = 0.05
) -> Callable[[Callable[P, Awaitable[T]]], Callable[P, Awaitable[T]]]:
    """Retry a coroutine on a transient deadlock/serialization failure.

    Only safe to wrap a function that owns its whole transaction (begins and
    commits/rolls back within the call), since a retry re-runs it from
    scratch.
    """

    def decorator(fn: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
        @functools.wraps(fn)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            for attempt in range(1, attempts + 1):
                try:
                    return await fn(*args, **kwargs)
                except DBAPIError as exc:
                    if _sqlstate(exc) not in _TRANSIENT_SQLSTATES or attempt == attempts:
                        raise
                    delay = base_delay_s * attempt + random.uniform(0, base_delay_s)
                    await asyncio.sleep(delay)
            raise AssertionError("unreachable")

        return wrapper

    return decorator
