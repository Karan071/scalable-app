"""Deadline propagation (LLD-13 §2.1).

The BFF mints an absolute epoch-ms deadline for the whole request and sends
it downstream as ``X-Request-Deadline``. Each hop computes its own
remaining budget from that same absolute value (not a relative timeout it
resets), so a slow early hop leaves less for every hop after it instead of
each one getting a fresh clock.
"""

import time
from collections.abc import Mapping

DEADLINE_HEADER = "x-request-deadline"
DEFAULT_SAFETY_MS = 50


class DeadlineExceeded(Exception):
    """Raised instead of making a call that has no realistic chance of
    finishing within the request's remaining budget (503 `deadline-
    exceeded`, LLD-13 §2.1 -- the caller maps this to that response)."""

    def __init__(self, remaining_ms: float) -> None:
        self.remaining_ms = remaining_ms
        super().__init__(f"deadline exceeded: {remaining_ms:.0f}ms remaining")


def new_deadline_ms(total_budget_s: float, *, now_ms: float | None = None) -> int:
    """Mints a fresh absolute deadline. Called once, at the edge (the BFF)."""
    base = now_ms if now_ms is not None else time.time() * 1000
    return int(base + total_budget_s * 1000)


def remaining_ms(
    deadline_ms: int, *, safety_ms: int = DEFAULT_SAFETY_MS, now_ms: float | None = None
) -> float:
    """Budget left after reserving ``safety_ms`` for this hop's own
    bookkeeping (serialization, queueing) -- can go negative."""
    base = now_ms if now_ms is not None else time.time() * 1000
    return deadline_ms - base - safety_ms


def require_remaining_ms(
    deadline_ms: int | None,
    *,
    minimum_ms: float,
    safety_ms: int = DEFAULT_SAFETY_MS,
    now_ms: float | None = None,
) -> float:
    """The remaining budget, or raises :class:`DeadlineExceeded` if it's
    below what this hop needs to even attempt the call. No deadline
    (``None``, e.g. a background job with nothing to propagate) means
    unbounded -- always passes."""
    if deadline_ms is None:
        return float("inf")
    remaining = remaining_ms(deadline_ms, safety_ms=safety_ms, now_ms=now_ms)
    if remaining < minimum_ms:
        raise DeadlineExceeded(remaining)
    return remaining


def read_deadline_header(headers: Mapping[str, str]) -> int | None:
    value = headers.get(DEADLINE_HEADER)
    return int(value) if value is not None else None


def deadline_header(deadline_ms: int) -> dict[str, str]:
    return {DEADLINE_HEADER: str(deadline_ms)}
