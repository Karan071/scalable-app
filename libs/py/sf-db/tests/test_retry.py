import pytest
from sf_db import retry_on_transient
from sqlalchemy.exc import DBAPIError


class _FakeDriverError(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


def _deadlock_error() -> DBAPIError:
    return DBAPIError.instance(
        statement="SELECT 1",
        params=None,
        orig=_FakeDriverError("40P01"),
        dbapi_base_err=Exception,
    )


def _non_transient_error() -> DBAPIError:
    return DBAPIError.instance(
        statement="SELECT 1",
        params=None,
        orig=_FakeDriverError("23505"),
        dbapi_base_err=Exception,
    )


async def test_retries_on_deadlock_then_succeeds() -> None:
    calls = {"n": 0}

    @retry_on_transient(attempts=3, base_delay_s=0.0)
    async def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 2:
            raise _deadlock_error()
        return "ok"

    assert await flaky() == "ok"
    assert calls["n"] == 2


async def test_gives_up_after_exhausting_attempts() -> None:
    calls = {"n": 0}

    @retry_on_transient(attempts=2, base_delay_s=0.0)
    async def always_deadlocks() -> None:
        calls["n"] += 1
        raise _deadlock_error()

    with pytest.raises(DBAPIError):
        await always_deadlocks()
    assert calls["n"] == 2


async def test_does_not_retry_non_transient_errors() -> None:
    calls = {"n": 0}

    @retry_on_transient(attempts=3, base_delay_s=0.0)
    async def unique_violation() -> None:
        calls["n"] += 1
        raise _non_transient_error()

    with pytest.raises(DBAPIError):
        await unique_violation()
    assert calls["n"] == 1
