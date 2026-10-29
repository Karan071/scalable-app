import pytest
from sf_common.deadlines import (
    DeadlineExceeded,
    deadline_header,
    new_deadline_ms,
    read_deadline_header,
    remaining_ms,
    require_remaining_ms,
)


def test_new_deadline_ms_is_total_budget_ahead_of_now() -> None:
    deadline = new_deadline_ms(3.0, now_ms=1_000_000.0)

    assert deadline == 1_000_000 + 3000


def test_remaining_ms_subtracts_elapsed_time_and_safety_margin() -> None:
    deadline = new_deadline_ms(3.0, now_ms=1_000_000.0)

    assert remaining_ms(deadline, now_ms=1_001_000.0) == 3000 - 1000 - 50


def test_require_remaining_ms_passes_when_enough_budget_remains() -> None:
    deadline = new_deadline_ms(3.0, now_ms=1_000_000.0)

    assert require_remaining_ms(deadline, minimum_ms=100, now_ms=1_001_000.0) == 1950


def test_require_remaining_ms_raises_when_budget_is_too_tight() -> None:
    deadline = new_deadline_ms(3.0, now_ms=1_000_000.0)

    with pytest.raises(DeadlineExceeded) as exc_info:
        require_remaining_ms(deadline, minimum_ms=2000, now_ms=1_001_000.0)
    assert exc_info.value.remaining_ms == 1950


def test_require_remaining_ms_is_unbounded_with_no_deadline() -> None:
    assert require_remaining_ms(None, minimum_ms=10_000_000) == float("inf")


def test_header_round_trip() -> None:
    deadline = new_deadline_ms(1.0, now_ms=1_000_000.0)

    headers = deadline_header(deadline)

    assert read_deadline_header(headers) == deadline


def test_read_deadline_header_is_none_when_absent() -> None:
    assert read_deadline_header({}) is None
