from collections.abc import AsyncIterator
from typing import Any

from fastapi import Request
from sf_db import build_engine, build_session_factory, transaction
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from .settings import Settings


def build_catalog_engine(settings: Settings, **pool: Any) -> AsyncEngine:
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


def build_catalog_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return build_session_factory(engine)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Per-request unit of work: commits on success, rolls back on an unhandled exception."""
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with transaction(factory) as session:
        yield session
