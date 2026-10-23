"""Turning database constraint violations into domain errors."""

from sqlalchemy.exc import IntegrityError

_UNIQUE_VIOLATION = "23505"


def violated_unique_constraint(exc: IntegrityError) -> str | None:
    """The name of the unique constraint/index ``exc`` violated, or ``None``
    if it's another kind of integrity error.

    Lets a service map *one specific* constraint to a client-facing conflict
    (409) while any other integrity error still surfaces as the bug it is.
    """
    if getattr(exc.orig, "sqlstate", None) != _UNIQUE_VIOLATION:
        return None
    # asyncpg's own exception, wrapped by SQLAlchemy's adapter, carries it.
    return getattr(getattr(exc.orig, "__cause__", None), "constraint_name", None)
