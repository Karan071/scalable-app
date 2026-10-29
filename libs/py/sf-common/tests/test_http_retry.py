import pytest
from sf_common.retry import RetryBudget, send_with_retries


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeResponse:
    def __init__(self, status_code: int, *, retry_after: str | None = None) -> None:
        self.status_code = status_code
        self.headers = {"retry-after": retry_after} if retry_after else {}


async def _no_sleep(_seconds: float) -> None:
    return None


def _unmetered_budget() -> RetryBudget:
    """A budget wide open for a single call's own retries -- for tests
    about ``max_attempts``/exception handling, not the budget itself
    (see test_retry_budget_* below for that, and
    test_exhausted_budget_stops_retrying_early for the interaction)."""
    return RetryBudget(retry_ratio=10.0)


def test_retry_budget_rejects_a_retry_with_no_requests_recorded() -> None:
    budget = RetryBudget(window_s=10.0, retry_ratio=0.1)

    assert budget.try_consume_retry() is False


def test_retry_budget_allows_retries_up_to_the_ratio() -> None:
    clock = FakeClock()
    budget = RetryBudget(window_s=10.0, retry_ratio=0.5, clock=clock)
    for _ in range(4):
        budget.record_request()

    assert budget.try_consume_retry() is True  # 1 retry / 4 requests = 25% < 50%
    assert budget.try_consume_retry() is True  # 2 / 4 = 50%, still allowed (>= not >)
    assert budget.try_consume_retry() is False  # 3 / 4 = 75%, over budget


def test_retry_budget_prunes_entries_outside_the_window() -> None:
    clock = FakeClock()
    budget = RetryBudget(window_s=10.0, retry_ratio=0.5, clock=clock)
    budget.record_request()
    budget.try_consume_retry()

    clock.advance(11.0)

    assert budget.try_consume_retry() is False  # no requests left in the window


async def test_non_retryable_status_returns_immediately() -> None:
    budget = RetryBudget()
    calls = 0

    async def send() -> FakeResponse:
        nonlocal calls
        calls += 1
        return FakeResponse(404)

    result = await send_with_retries(send, idempotent=True, budget=budget, sleep=_no_sleep)

    assert result.status_code == 404
    assert calls == 1


async def test_retryable_status_is_retried_when_idempotent() -> None:
    budget = _unmetered_budget()
    calls = 0

    async def send() -> FakeResponse:
        nonlocal calls
        calls += 1
        return FakeResponse(503) if calls < 3 else FakeResponse(200)

    result = await send_with_retries(
        send, idempotent=True, budget=budget, max_attempts=3, sleep=_no_sleep
    )

    assert result.status_code == 200
    assert calls == 3


async def test_retryable_status_is_not_retried_when_not_idempotent() -> None:
    budget = RetryBudget()
    calls = 0

    async def send() -> FakeResponse:
        nonlocal calls
        calls += 1
        return FakeResponse(503)

    result = await send_with_retries(send, idempotent=False, budget=budget, sleep=_no_sleep)

    assert result.status_code == 503
    assert calls == 1


async def test_gives_up_after_max_attempts() -> None:
    budget = _unmetered_budget()
    calls = 0

    async def send() -> FakeResponse:
        nonlocal calls
        calls += 1
        return FakeResponse(503)

    result = await send_with_retries(
        send, idempotent=True, budget=budget, max_attempts=3, sleep=_no_sleep
    )

    assert result.status_code == 503
    assert calls == 3


async def test_exhausted_budget_stops_retrying_early() -> None:
    clock = FakeClock()
    budget = RetryBudget(window_s=10.0, retry_ratio=0.1, clock=clock)
    # Already over ratio (1 retry / 1 request = 100% > 10%) before this call
    # even makes its own request -- deterministic, unlike relying on a
    # fractional ratio against a tiny absolute count.
    budget.record_request()
    budget.try_consume_retry()
    calls = 0

    async def send() -> FakeResponse:
        nonlocal calls
        calls += 1
        return FakeResponse(503)

    result = await send_with_retries(
        send, idempotent=True, budget=budget, max_attempts=5, sleep=_no_sleep
    )

    assert result.status_code == 503
    assert calls == 1  # the budget had no room for even one retry


async def test_retryable_exception_is_retried_then_raised_if_never_resolved() -> None:
    budget = _unmetered_budget()
    calls = 0

    async def send() -> FakeResponse:
        nonlocal calls
        calls += 1
        raise ConnectionError("downstream unreachable")

    with pytest.raises(ConnectionError):
        await send_with_retries(
            send,
            idempotent=True,
            budget=budget,
            retryable_errors=(ConnectionError,),
            max_attempts=3,
            sleep=_no_sleep,
        )

    assert calls == 3


async def test_non_retryable_exception_raises_immediately() -> None:
    budget = RetryBudget()
    calls = 0

    async def send() -> FakeResponse:
        nonlocal calls
        calls += 1
        raise ValueError("not a retryable kind of broken")

    with pytest.raises(ValueError):
        await send_with_retries(
            send,
            idempotent=True,
            budget=budget,
            retryable_errors=(ConnectionError,),
            sleep=_no_sleep,
        )

    assert calls == 1


async def test_retry_after_header_extends_the_backoff() -> None:
    budget = RetryBudget()
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    calls = 0

    async def send() -> FakeResponse:
        nonlocal calls
        calls += 1
        return FakeResponse(429, retry_after="5") if calls == 1 else FakeResponse(200)

    await send_with_retries(send, idempotent=True, budget=budget, max_attempts=3, sleep=sleep)

    assert slept == [5.0]  # Retry-After (5s) dominates the jittered backoff (<=1s)
