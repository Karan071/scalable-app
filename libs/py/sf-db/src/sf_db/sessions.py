"""Session factory and transaction boundary (LLD-09 §3: no network calls inside a transaction)."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def transaction(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Open a session and commit/roll back as a single unit of work.

    Callers must not await any outbound HTTP or broker call while inside this
    block (LLD-09 §3): it holds a pooled connection for the duration.
    """
    async with session_factory() as session, session.begin():
        yield session
