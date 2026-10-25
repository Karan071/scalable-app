from collections.abc import AsyncIterator
from typing import Any

from fastapi import Request
from sf_db import build_engine, build_session_factory, transaction
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from .settings import Settings


def build_orders_engine(settings: Settings, **pool: Any) -> AsyncEngine:
    """``pool`` overrides the request-handling defaults, e.g. ``**worker_pool(n)``
    for a background worker's own engine (LLD-09 §2 bulkheads)."""
    return build_engine(
        host=settings.db_host,
        port=settings.db_port,
        database=settings.db_name,
        user=settings.db_user,
        password=settings.db_password,
        **pool,
    )


def build_orders_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return build_session_factory(engine)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Per-request unit of work for simple reads/single-row writes."""
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with transaction(factory) as session:
        yield session


def get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    """For checkout/saga-step operations (service.py): each owns its whole
    transaction so ``sf_db.retry_on_transient`` can re-run it from scratch."""
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    return factory
