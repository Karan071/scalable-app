"""Async engine construction (ADR-0006, LLD-09 §2).

One engine per connection pool. Services create a separate engine for request
handling and one for each background worker (relay, sweepers), so a backlog
in one cannot starve the other (the "bulkhead" row in LLD-09 §2).
"""

from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from sqlalchemy import URL
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

# What a pool raises when no connection frees up within pool_timeout. Pass to
# sf_common.install_problem_details(db_busy_errors=...) for 503 db-busy.
POOL_TIMEOUT_ERRORS: tuple[type[Exception], ...] = (PoolTimeoutError,)


def build_engine(
    *,
    host: str,
    port: int,
    database: str,
    user: str,
    password: str,
    pool_size: int = 10,
    max_overflow: int = 5,
    pool_timeout: float = 2.0,
    pool_recycle: int = 1800,
) -> AsyncEngine:
    """Build an asyncpg engine with the M2 pool defaults (LLD-09 §2).

    ``pool_timeout`` fails fast on exhaustion rather than queuing requests
    indefinitely; callers turn the resulting ``TimeoutError`` into a 503.
    """
    url = URL.create(
        "postgresql+asyncpg",
        username=user,
        password=password,
        host=host,
        port=port,
        database=database,
    )
    engine = create_async_engine(
        url,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_timeout=pool_timeout,
        pool_pre_ping=True,
        pool_recycle=pool_recycle,
    )
    # `db.statement` is captured sanitized (parameters are never recorded) --
    # that's the instrumentation's own default behavior, not opt-in here.
    SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine)
    return engine


def worker_pool(size: int) -> dict[str, float | int]:
    """Pool settings for a background worker's own engine (LLD-09 §2 bulkheads).

    Sized to the worker's own concurrency, so it never needs overflow; and it
    waits for a connection rather than failing fast like request handling,
    because a background worker should apply backpressure, not error. Pass
    as ``build_engine(..., **worker_pool(n))``.
    """
    return {"pool_size": size, "max_overflow": 0, "pool_timeout": 30.0}
