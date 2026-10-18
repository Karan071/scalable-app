"""Redis pub/sub + polling fallback, debounced/single-flight reload (ADR-0004 §4, LLD-02 §8)."""

import asyncio
import contextlib
import logging

from redis.asyncio import Redis
from redis.asyncio.client import PubSub

from .enforcer import PolicyEnforcer

logger = logging.getLogger("sf_authz.watcher")

DEFAULT_CHANNEL = "authz:policy"
DEFAULT_POLL_INTERVAL_S = 10.0
DEFAULT_DEBOUNCE_S = 0.2


class PolicyWatcher:
    """Two independent propagation paths: a redis-cache subscription and a
    fixed-interval poll, so policy still propagates (just slower) if Redis
    is unreachable (LLD-02 §8's "polling fallback" / "redis-cache down" row).
    """

    def __init__(
        self,
        *,
        enforcer: PolicyEnforcer,
        redis_client: Redis | None,
        channel: str = DEFAULT_CHANNEL,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        debounce_s: float = DEFAULT_DEBOUNCE_S,
    ) -> None:
        self._enforcer = enforcer
        self._redis = redis_client
        self._channel = channel
        self._poll_interval_s = poll_interval_s
        self._debounce_s = debounce_s
        self._tasks: list[asyncio.Task] = []
        self._debounce_task: asyncio.Task | None = None
        self._reload_lock = asyncio.Lock()
        self._pubsub: PubSub | None = None
        self._stopped = False

    async def start(self) -> None:
        """Subscribes (and awaits the subscription actually landing) before
        returning whenever Redis is reachable — a caller that bumps
        ``policy_version`` and publishes right after ``start()`` must not
        be able to race ahead of it and have the notification silently
        dropped (confirmed reproducible against real redis-cache without
        this). If Redis is unreachable at startup, this must NOT raise —
        that would crash the whole service even though the entire point of
        the polling fallback (LLD-02 §8) is to keep working without it;
        ``_redis_loop`` retries the subscribe on its own schedule instead.
        """
        self._stopped = False
        if self._redis is not None:
            try:
                pubsub = self._redis.pubsub()
                await pubsub.subscribe(self._channel)
                self._pubsub = pubsub
            except Exception:
                logger.exception("initial redis subscribe failed; polling fallback will retry")
                self._pubsub = None
            self._tasks.append(asyncio.create_task(self._redis_loop()))
        self._tasks.append(asyncio.create_task(self._poll_loop()))

    async def stop(self) -> None:
        self._stopped = True
        all_tasks = [*self._tasks]
        if self._debounce_task is not None:
            all_tasks.append(self._debounce_task)
        for task in all_tasks:
            task.cancel()
        for task in all_tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        self._debounce_task = None
        if self._pubsub is not None:
            with contextlib.suppress(Exception):
                await self._pubsub.aclose()
            self._pubsub = None

    def _signal(self, version: int) -> None:
        if version <= self._enforcer.current_version:
            return
        if self._debounce_task is not None and not self._debounce_task.done():
            # Already waiting to reload (or a reload is in flight under the
            # same task handle — it spans the debounce sleep *and* the
            # reload call). Don't cancel it: that would abort an in-flight
            # reload rather than just a pending wait. Let it fire once.
            return
        self._debounce_task = asyncio.create_task(self._debounced_reload())

    async def _debounced_reload(self) -> None:
        await asyncio.sleep(self._debounce_s)
        if self._reload_lock.locked():
            # Single-flight: a reload already in progress will pick up the
            # latest version anyway once it completes.
            return
        async with self._reload_lock:
            try:
                await self._enforcer.reload()
            except Exception:
                logger.exception("policy reload failed")

    async def _redis_loop(self) -> None:
        assert self._redis is not None
        while not self._stopped:
            if self._pubsub is None:
                try:
                    pubsub = self._redis.pubsub()
                    await pubsub.subscribe(self._channel)
                    self._pubsub = pubsub
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("redis (re)subscribe failed; polling fallback continues")
                    await asyncio.sleep(1.0)
                    continue
            try:
                async for message in self._pubsub.listen():
                    if message["type"] == "message":
                        self._signal(int(message["data"]))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("redis watcher connection lost; polling fallback continues")
                with contextlib.suppress(Exception):
                    await self._pubsub.aclose()
                self._pubsub = None
                await asyncio.sleep(1.0)

    async def _poll_loop(self) -> None:
        while not self._stopped:
            await asyncio.sleep(self._poll_interval_s)
            try:
                version = await self._enforcer.fetch_version_only()
                self._signal(version)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("policy_version poll failed")
