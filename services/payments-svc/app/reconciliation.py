"""R2: unknown-payment reconciliation (LLD-06 §9). Runs in every replica,
every ``interval_s``.
"""

import asyncio
import contextlib
import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from . import service
from .provider_client import MockProviderClient

logger = logging.getLogger("payments_svc.reconciliation")


class UnknownPaymentReconciler:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        provider: MockProviderClient,
        interval_s: float,
        unknown_after_s: float,
    ) -> None:
        self._session_factory = session_factory
        self._provider = provider
        self._interval_s = interval_s
        self._unknown_after_s = unknown_after_s
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
                resolved = await service.reconcile_unknown_attempts(
                    self._session_factory, self._provider, older_than_s=self._unknown_after_s
                )
                if resolved:
                    logger.info("R2 reconciliation resolved %d unknown attempt(s)", resolved)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("R2 reconciliation tick failed; retrying next interval")
