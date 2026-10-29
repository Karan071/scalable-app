from collections.abc import AsyncIterator
from typing import Any

from fastapi import Request
from sf_common import CacheClient
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


def get_product_cache(request: Request) -> CacheClient:
    """LLD-09 §6.1's storefront-product cache. A route invalidates it via
    ``BackgroundTasks`` (routes.py), never inline in the request body:
    ``get_session``'s own docstring forbids awaiting an outbound call
    (Redis included) while its ``session.begin()`` holds a pooled
    connection open, and a background task only runs once the response
    (and that dependency's own commit) is already done."""
    return request.app.state.product_cache  # type: ignore[no-any-return]
