"""Transactional outbox relay (LLD-08 §5.3).

Runs in every replica of a producing service. Claims unpublished outbox
rows, publishes each to the broker with publisher confirms, and marks
confirmed rows published — all in one DB transaction per tick. Woken by
either a Postgres ``NOTIFY`` (fast path) or a fixed poll interval
(fallback, so a missed/late notify never stalls the relay indefinitely).
"""

import asyncio
import contextlib
import json
import logging
import random
import re
import time
from datetime import UTC, datetime
from typing import Any

import aio_pika
import asyncpg
from opentelemetry import trace
from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from . import metrics as _metrics

logger = logging.getLogger("sf_messaging.relay")
_tracer = trace.get_tracer(__name__)

#: Table/channel names are interpolated into raw SQL (can't be bind params),
#: so they're restricted the same way outbox.py restricts NOTIFY channels —
#: always a literal constant in trusted service code, never request input,
#: but validated anyway for defense in depth.
_VALID_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")

DEFAULT_BATCH_SIZE = 200
DEFAULT_POLL_INTERVAL_S = 0.25
DEFAULT_CONFIRM_TIMEOUT_S = 5.0
DEFAULT_BACKLOG_REFRESH_S = 5.0
# The relay's loop uses one session at a time (claim+publish+mark, then the
# backlog refresh), so its own engine needs only this many connections.
RELAY_POOL_SIZE = 2

_EVENTS_EXCHANGE = "sf.events"
_AMQP_NO_ROUTE = 312

# `attempts` first: a row that keeps failing sinks behind every fresh one,
# so a full batch of failures can never starve the rest of the outbox.
# Ordering by `occurred_at` alone let 200 unroutable rows block orgs-svc's
# relay completely (2026-10-21 .. 10-23).
_CLAIM_SQL_TEMPLATE = """
    SELECT id, exchange, routing_key, payload, occurred_at
    FROM {table}
    WHERE published_at IS NULL
    ORDER BY attempts, occurred_at, id
    LIMIT :batch_size
    FOR UPDATE SKIP LOCKED
"""

_MARK_PUBLISHED_SQL_TEMPLATE = "UPDATE {table} SET published_at = now() WHERE id IN :ids"

_MARK_FAILED_SQL_TEMPLATE = (
    "UPDATE {table} SET attempts = attempts + 1, last_error = :error WHERE id = :id"
)

# Served by the partial index on unpublished rows (LLD-08 §5.1).
_BACKLOG_SQL_TEMPLATE = "SELECT count(*), min(occurred_at) FROM {table} WHERE published_at IS NULL"


class OutboxRelay:
    """One instance per producing service process."""

    def __init__(
        self,
        *,
        service_name: str,
        session_factory: async_sessionmaker[AsyncSession],
        dsn_asyncpg: str,
        amqp_url: str,
        notify_channel: str,
        outbox_table: str = "outbox",
        batch_size: int = DEFAULT_BATCH_SIZE,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        confirm_timeout_s: float = DEFAULT_CONFIRM_TIMEOUT_S,
        backlog_refresh_s: float = DEFAULT_BACKLOG_REFRESH_S,
    ) -> None:
        if not _VALID_IDENTIFIER.match(notify_channel):
            raise ValueError(f"not a safe NOTIFY channel name: {notify_channel!r}")
        if not _VALID_IDENTIFIER.match(outbox_table):
            raise ValueError(f"not a safe table name: {outbox_table!r}")

        self._service_name = service_name
        self._session_factory = session_factory
        self._dsn_asyncpg = dsn_asyncpg
        self._amqp_url = amqp_url
        self._notify_channel = notify_channel
        self._outbox_table = outbox_table
        self._batch_size = batch_size
        self._poll_interval_s = poll_interval_s
        self._confirm_timeout_s = confirm_timeout_s
        self._backlog_refresh_s = backlog_refresh_s
        self._backlog_refreshed_at = 0.0

        self._claim_sql = text(_CLAIM_SQL_TEMPLATE.format(table=outbox_table))
        self._mark_published_sql = text(
            _MARK_PUBLISHED_SQL_TEMPLATE.format(table=outbox_table)
        ).bindparams(bindparam("ids", expanding=True))
        self._mark_failed_sql = text(_MARK_FAILED_SQL_TEMPLATE.format(table=outbox_table))
        self._backlog_sql = text(_BACKLOG_SQL_TEMPLATE.format(table=outbox_table))

        self._wake = asyncio.Event()
        self._stopped = False
        self._listen_conn: asyncpg.Connection | None = None
        self._amqp_connection: aio_pika.abc.AbstractRobustConnection | None = None
        self._amqp_channel: aio_pika.abc.AbstractChannel | None = None
        self._exchange_cache: dict[str, aio_pika.abc.AbstractExchange] = {}
        self._tasks: list[asyncio.Task] = []
        self.is_running = False

    async def start(self) -> None:
        self._stopped = False
        self._amqp_connection = await aio_pika.connect_robust(self._amqp_url)
        self._amqp_channel = await self._amqp_connection.channel(
            publisher_confirms=True, on_return_raises=True
        )
        self._tasks.append(asyncio.create_task(self._listen_loop()))
        self._tasks.append(asyncio.create_task(self._main_loop()))
        self.is_running = True

    async def stop(self) -> None:
        self._stopped = True
        self.is_running = False
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        if self._listen_conn is not None:
            with contextlib.suppress(Exception):
                await self._listen_conn.close()
            self._listen_conn = None
        if self._amqp_connection is not None:
            with contextlib.suppress(Exception):
                await self._amqp_connection.close()
            self._amqp_connection = None

    # --- LISTEN wiring (dedicated non-pooled connection; a pool must never
    # hold a session open indefinitely the way LISTEN requires) -------------

    async def _listen_loop(self) -> None:
        backoff_s = 1.0
        while not self._stopped:
            try:
                conn = await asyncpg.connect(dsn=self._dsn_asyncpg)
                await conn.add_listener(self._notify_channel, self._on_notify)
                self._listen_conn = conn
                backoff_s = 1.0
                while not self._stopped and not conn.is_closed():
                    await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("outbox relay LISTEN connection failed; retrying")
                await asyncio.sleep(backoff_s + random.uniform(0, backoff_s))
                backoff_s = min(backoff_s * 2, 30.0)

    def _on_notify(self, *_args: Any) -> None:
        self._wake.set()

    # --- main claim/publish loop --------------------------------------------

    async def _main_loop(self) -> None:
        while not self._stopped:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._poll_interval_s)
            except TimeoutError:
                pass
            self._wake.clear()  # clear before processing: avoid a missed-notify race
            try:
                await self._drain_once()
                await self._maybe_refresh_backlog()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("outbox relay batch failed; backing off")
                await asyncio.sleep(1.0)

    async def _maybe_refresh_backlog(self) -> None:
        now = time.monotonic()
        if now - self._backlog_refreshed_at < self._backlog_refresh_s:
            return
        self._backlog_refreshed_at = now
        async with self._session_factory() as session:
            pending, oldest = (await session.execute(self._backlog_sql)).one()
        _metrics.record_backlog(
            self._service_name, pending=int(pending), oldest_unpublished_at=oldest
        )

    async def _drain_once(self) -> None:
        # One span per batch tick, not per row (LLD-10 §2) -- each row's own
        # publish already carries its own producer-side trace context
        # forward via the AMQP headers in _publish_one, independent of this
        # relay-tick span, which is purely an operational (not business) one.
        with _tracer.start_as_current_span(
            "outbox.relay.batch", attributes={"service.name": self._service_name}
        ) as span:
            async with self._session_factory() as session, session.begin():
                rows = (
                    (await session.execute(self._claim_sql, {"batch_size": self._batch_size}))
                    .mappings()
                    .all()
                )
                span.set_attribute("outbox.batch_size", len(rows))
                if not rows:
                    return

                confirmed_ids: list[Any] = []
                for row in rows:
                    error = await self._publish_one(row)
                    if error is None:
                        confirmed_ids.append(row["id"])
                    else:
                        await session.execute(
                            self._mark_failed_sql, {"id": row["id"], "error": error[:500]}
                        )
                if confirmed_ids:
                    await session.execute(self._mark_published_sql, {"ids": confirmed_ids})
                span.set_attribute("outbox.confirmed", len(confirmed_ids))

    async def _publish_one(self, row: Any) -> str | None:
        """Returns None on success, else an error string (row stays unpublished)."""
        payload = row["payload"]
        try:
            exchange = await self._get_exchange(row["exchange"])
            message = aio_pika.Message(
                body=json.dumps(payload).encode(),
                message_id=str(row["id"]),
                content_type="application/json",
                type=payload.get("type"),
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                headers={
                    "traceparent": payload.get("traceparent") or "",
                    "tracestate": payload.get("tracestate") or "",
                },
            )
            await exchange.publish(
                message,
                routing_key=row["routing_key"],
                mandatory=True,
                timeout=self._confirm_timeout_s,
            )
            self._count("confirmed")
            _metrics.outbox_publish_latency_seconds.record(
                max((datetime.now(UTC) - row["occurred_at"]).total_seconds(), 0.0),
                {"service": self._service_name},
            )
            return None
        except aio_pika.exceptions.DeliveryError as exc:
            returned = getattr(exc.frame, "reply_code", None) == _AMQP_NO_ROUTE
            if row["exchange"] == _EVENTS_EXCHANGE and returned:
                self._count("no_subscriber")
                # An event with no subscriber yet (e.g. invitation.created
                # before notifications-svc exists) is ordinary pub/sub, not a
                # failure: done, not retried. Commands still fail on NO_ROUTE
                # below -- a command always has exactly one intended consumer,
                # so an unbound one is a real topology bug (LLD-08 §5.3).
                logger.warning(
                    "outbox relay: no queue bound for event %s (row %s); marked published",
                    row["routing_key"],
                    row["id"],
                )
                return None
            # A nack, or an unroutable command: keep retrying, log loudly.
            self._count("returned" if returned else "nacked")
            logger.error("outbox relay delivery failed for %s: %s", row["id"], exc)
            return f"delivery_error: {exc}"
        except Exception as exc:
            self._count("error")
            logger.exception("outbox relay publish failed for %s", row["id"])
            return str(exc)

    def _count(self, result: str) -> None:
        _metrics.outbox_publish_total.add(1, {"service": self._service_name, "result": result})

    async def _get_exchange(self, name: str) -> aio_pika.abc.AbstractExchange:
        if name not in self._exchange_cache:
            assert self._amqp_channel is not None
            self._exchange_cache[name] = await self._amqp_channel.declare_exchange(
                name, type="topic", durable=True, passive=True
            )
        return self._exchange_cache[name]
