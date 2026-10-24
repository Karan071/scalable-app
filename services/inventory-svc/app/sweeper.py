"""Reservation expiry sweeper (LLD-07 §6): runs in every replica, every
``interval_s``, batching up to ``batch_size`` expired reservations per tick.
"""

import asyncio
import contextlib
import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from . import service

logger = logging.getLogger("inventory_svc.sweeper")


class ExpirySweeper:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        interval_s: float,
        batch_size: int,
    ) -> None:
        self._session_factory = session_factory
        self._interval_s = interval_s
        self._batch_size = batch_size
        self._task: asyncio.Task | None = None
        self.is_running = False

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())
        self.is_running = True

    async def stop(self) -> None:
        self.is_running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval_s)
            try:
                # A full batch means more are likely waiting; drain before
                # sleeping again rather than waiting a full tick per batch.
                while (
                    await service.expire_batch(self._session_factory, limit=self._batch_size)
                    == self._batch_size
                ):
                    pass
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("expiry sweeper batch failed; retrying next tick")
