import pytest
from sf_common.circuit_breaker import CircuitBreaker, CircuitOpenError, CircuitState


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _breaker(clock: FakeClock, **overrides: object) -> CircuitBreaker:
    kwargs: dict[str, object] = dict(
        service="orders-svc",
        dependency="payments-svc",
        slow_call_duration_s=1.0,
        window_s=10.0,
        min_calls=10,
        failure_rate_threshold=0.5,
        slow_call_rate_threshold=0.8,
        open_duration_s=15.0,
        half_open_probes=3,
        clock=clock,
    )
    kwargs.update(overrides)
    return CircuitBreaker(**kwargs)  # type: ignore[arg-type]


async def _ok() -> str:
    return "ok"


async def _fail() -> str:
    raise RuntimeError("downstream exploded")


async def test_stays_closed_below_the_minimum_call_count() -> None:
    clock = FakeClock()
    breaker = _breaker(clock, min_calls=10)

    for _ in range(9):
        with pytest.raises(RuntimeError):
            await breaker.call(_fail)

    assert breaker.state is CircuitState.CLOSED


async def test_opens_once_the_failure_rate_crosses_the_threshold() -> None:
    clock = FakeClock()
    breaker = _breaker(clock, min_calls=10, failure_rate_threshold=0.5)

    for _ in range(5):
        await breaker.call(_ok)
    for _ in range(5):
        with pytest.raises(RuntimeError):
            await breaker.call(_fail)

    assert breaker.state is CircuitState.OPEN


async def test_opens_on_a_slow_call_rate_even_with_no_failures() -> None:
    clock = FakeClock()
    # A wide window: 10 calls at 2s apart span 20s, longer than the default
    # 10s window would hold -- the early ones would age out before the 10th
    # call's own check, which is a real (and correct) rolling-window
    # interaction, just not what this test means to exercise.
    breaker = _breaker(
        clock,
        min_calls=10,
        window_s=30.0,
        slow_call_duration_s=1.0,
        slow_call_rate_threshold=0.8,
    )

    async def slow() -> str:
        clock.advance(2.0)  # longer than slow_call_duration_s
        return "ok"

    for _ in range(10):
        await breaker.call(slow)

    assert breaker.state is CircuitState.OPEN


async def test_open_breaker_fails_fast_without_calling_the_function() -> None:
    clock = FakeClock()
    breaker = _breaker(clock, min_calls=2, failure_rate_threshold=0.5)
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await breaker.call(_fail)
    assert breaker.state is CircuitState.OPEN

    called = False

    async def tracked() -> str:
        nonlocal called
        called = True
        return "ok"

    with pytest.raises(CircuitOpenError):
        await breaker.call(tracked)
    assert called is False


async def test_half_open_after_the_open_duration_elapses() -> None:
    clock = FakeClock()
    breaker = _breaker(clock, min_calls=2, failure_rate_threshold=0.5, open_duration_s=15.0)
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await breaker.call(_fail)
    assert breaker.state is CircuitState.OPEN

    clock.advance(15.0)
    await breaker.call(_ok)

    assert breaker.state is CircuitState.HALF_OPEN


async def test_half_open_closes_once_every_probe_succeeds() -> None:
    clock = FakeClock()
    breaker = _breaker(clock, min_calls=2, failure_rate_threshold=0.5, half_open_probes=3)
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await breaker.call(_fail)
    clock.advance(15.0)

    await breaker.call(_ok)
    await breaker.call(_ok)
    assert breaker.state is CircuitState.HALF_OPEN
    await breaker.call(_ok)

    assert breaker.state is CircuitState.CLOSED


async def test_half_open_reopens_on_any_probe_failure() -> None:
    clock = FakeClock()
    breaker = _breaker(clock, min_calls=2, failure_rate_threshold=0.5, half_open_probes=3)
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await breaker.call(_fail)
    clock.advance(15.0)

    with pytest.raises(RuntimeError):
        await breaker.call(_fail)

    assert breaker.state is CircuitState.OPEN
