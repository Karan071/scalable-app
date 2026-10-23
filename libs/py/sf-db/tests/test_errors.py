from sf_db import violated_unique_constraint
from sqlalchemy.exc import IntegrityError


class _AsyncpgError(Exception):
    def __init__(self, constraint_name: str | None) -> None:
        super().__init__(constraint_name)
        self.constraint_name = constraint_name


class _AdaptedError(Exception):
    """Mirrors SQLAlchemy's asyncpg adapter: sqlstate on the adapted error,
    the constraint name on the asyncpg exception it wraps."""

    def __init__(self, sqlstate: str, constraint_name: str | None) -> None:
        super().__init__(sqlstate)
        self.sqlstate = sqlstate
        self.__cause__ = _AsyncpgError(constraint_name)


def _integrity_error(sqlstate: str, constraint_name: str | None) -> IntegrityError:
    return IntegrityError("INSERT ...", None, _AdaptedError(sqlstate, constraint_name))


def test_returns_the_violated_unique_constraint() -> None:
    exc = _integrity_error("23505", "uq_organizations_slug")
    assert violated_unique_constraint(exc) == "uq_organizations_slug"


def test_other_integrity_errors_are_not_unique_violations() -> None:
    # 23503 = foreign_key_violation: must not be mistaken for a conflict.
    assert violated_unique_constraint(_integrity_error("23503", "fk_something")) is None
