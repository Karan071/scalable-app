import pytest
from sf_common.bulkhead import Bulkhead, BulkheadFullError


async def test_try_acquire_rejects_once_full() -> None:
    bulkhead = Bulkhead(max_concurrency=1)

    assert await bulkhead.try_acquire() is True
    assert await bulkhead.try_acquire() is False


async def test_release_frees_a_slot() -> None:
    bulkhead = Bulkhead(max_concurrency=1)
    await bulkhead.try_acquire()

    await bulkhead.release()

    assert await bulkhead.try_acquire() is True


async def test_in_flight_tracks_current_holders() -> None:
    bulkhead = Bulkhead(max_concurrency=5)
    await bulkhead.try_acquire()
    await bulkhead.try_acquire()

    assert bulkhead.in_flight == 2


async def test_guard_releases_even_if_the_body_raises() -> None:
    bulkhead = Bulkhead(max_concurrency=1)

    with pytest.raises(RuntimeError):
        async with bulkhead.guard():
            raise RuntimeError("boom")

    assert bulkhead.in_flight == 0


async def test_guard_raises_bulkhead_full_without_entering_the_body() -> None:
    bulkhead = Bulkhead(max_concurrency=0)
    entered = False

    with pytest.raises(BulkheadFullError):
        async with bulkhead.guard():
            entered = True

    assert entered is False
