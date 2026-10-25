"""The checkout saga recovery worker (LLD-06 §5.3): runs in every replica,
every ``interval_s``, claiming due sagas and re-driving (or compensating)
each one. See ``service.recover_one``'s docstring for exactly what it can
and can't do without the original request in hand.
"""

import asyncio
import contextlib
import logging

from sf_db import transaction
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from . import repository, service

logger = logging.getLogger("orders_svc.recovery")


class RecoveryWorker:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        participants: service.Participants,
        interval_s: float,
        batch_size: int,
    ) -> None:
        self._session_factory = session_factory
        self._participants = participants
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
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("recovery tick failed; retrying next interval")

    async def _tick(self) -> None:
        async with transaction(self._session_factory) as session:
            order_ids = await repository.claim_due_sagas(session, limit=self._batch_size)
        for order_id in order_ids:
            try:
                await service.recover_one(
                    self._session_factory, self._participants, order_id=order_id
                )
            except Exception:
                logger.exception("recovery step failed for order %s", order_id)
