from collections.abc import AsyncIterator
from typing import Any

from fastapi import Request
from sf_authz import PolicyEnforcer, build_enforcer
from sf_db import build_engine, build_session_factory, transaction
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from .settings import Settings


def build_audit_engine(settings: Settings, **pool: Any) -> AsyncEngine:
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


def build_audit_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return build_session_factory(engine)


def build_audit_reader_enforcer(settings: Settings) -> PolicyEnforcer:
    """``authz_reader``-only: policy load/reload, never a write path (ADR-0004 §2)."""
    return build_enforcer(
        db_host=settings.db_host,
        db_port=settings.db_port,
        db_name=settings.db_name,
        db_user=settings.authz_reader_db_user,
        db_password=settings.authz_reader_db_password,
    )


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with transaction(factory) as session:
        yield session
