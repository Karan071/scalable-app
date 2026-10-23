"""Idempotent inbox-pattern consumer base (LLD-08 §6).

On each message: dedupe via an ``INSERT ... ON CONFLICT DO NOTHING`` into
the consumer's own ``inbox`` table, then dispatch to a handler keyed by the
envelope's ``type`` — all inside one transaction. A missing handler for a
type is a no-op (not an error): a queue bound on a wildcard routing key
(e.g. ``organization.*.v1``) will receive types this consumer doesn't act
on. Any handler exception nacks the message without requeue — LLD-08 §7's
retry tiers and DLQ tooling are a separate, later epic (E4.5); for now a
failing message goes straight to whatever the queue is configured to
dead-letter into.
"""

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from uuid import UUID

import aio_pika
from opentelemetry import propagate, trace
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from . import metrics as _metrics

logger = logging.getLogger("sf_messaging.consumer")
_tracer = trace.get_tracer(__name__)

DEFAULT_PREFETCH = 20
# LLD-08 §6: "prefetch 20 per channel and a per-pod handler concurrency of
# 10". The cap bounds how many inbox transactions (pooled connections) one
# consumer holds at once: size its engine with sf_db.worker_pool(this).
DEFAULT_HANDLER_CONCURRENCY = 10

_VALID_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")

HandlerFn = Callable[[AsyncSession, dict[str, Any]], Awaitable[Any]]
OnHandledFn = Callable[[Any], Awaitable[None]]


class InboxConsumer:
    """One instance per logical consumer. ``consumer_name`` must be stable
    across replicas (it's the inbox dedup key's discriminator, not a
    per-process identifier) — any replica must dedupe against the same row.
    """

    def __init__(
        self,
        *,
        consumer_name: str,
        session_factory: async_sessionmaker[AsyncSession],
        amqp_url: str,
        queue_name: str,
        handlers: Mapping[str, HandlerFn],
        prefetch: int = DEFAULT_PREFETCH,
        handler_concurrency: int = DEFAULT_HANDLER_CONCURRENCY,
        inbox_table: str = "inbox",
        on_handled: OnHandledFn | None = None,
    ) -> None:
        if not _VALID_IDENTIFIER.match(inbox_table):
            raise ValueError(f"not a safe table name: {inbox_table!r}")

        self._consumer_name = consumer_name
        self._session_factory = session_factory
        self._amqp_url = amqp_url
        self._queue_name = queue_name
        self._handlers = handlers
        self._prefetch = prefetch
        self._slots = asyncio.Semaphore(handler_concurrency)
        self._on_handled = on_handled

        self._insert_inbox_sql = text(
            f"INSERT INTO {inbox_table} (consumer, message_id) VALUES (:consumer, :message_id) "
            "ON CONFLICT DO NOTHING RETURNING message_id"
        )

        self._connection: aio_pika.abc.AbstractRobustConnection | None = None
        self._channel: aio_pika.abc.AbstractChannel | None = None
        self._consumer_tag: aio_pika.abc.ConsumerTag | None = None
        self.is_running = False

    async def start(self) -> None:
        self._connection = await aio_pika.connect_robust(self._amqp_url)
        self._channel = await self._connection.channel()
        await self._channel.set_qos(prefetch_count=self._prefetch)
        queue = await self._channel.declare_queue(self._queue_name, durable=True, passive=True)
        self._consumer_tag = await queue.consume(self._on_message, no_ack=False)
        self.is_running = True

    async def stop(self) -> None:
        self.is_running = False
        if self._connection is not None:
            await self._connection.close()
            self._connection = None
            self._channel = None
            self._consumer_tag = None

    async def _on_message(self, message: aio_pika.abc.AbstractIncomingMessage) -> None:
        async with self._slots:
            await self._handle(message)

    async def _handle(self, message: aio_pika.abc.AbstractIncomingMessage) -> None:
        started = time.monotonic()
        event_type = "<unparsed>"
        message_id: UUID | None = None
        try:
            envelope = json.loads(message.body)
            event_type = envelope["type"]
            message_id = UUID(envelope["id"])

            # Links this span back to the producer's request span (LLD-08
            # §10) via whatever real traceparent build_envelope() captured
            # at write time — empty/missing just starts a fresh root span.
            carrier = {
                "traceparent": envelope.get("traceparent") or "",
                "tracestate": envelope.get("tracestate") or "",
            }
            parent_ctx = propagate.extract(carrier)

            handled_value: Any = None
            is_duplicate = False
            with _tracer.start_as_current_span(
                f"event.consume.{event_type}",
                context=parent_ctx,
                attributes={"messaging.consumer": self._consumer_name},
            ):
                async with self._session_factory() as session, session.begin():
                    result = await session.execute(
                        self._insert_inbox_sql,
                        {"consumer": self._consumer_name, "message_id": message_id},
                    )
                    if result.first() is None:
                        is_duplicate = True
                    else:
                        handler = self._handlers.get(event_type)
                        if handler is not None:
                            handled_value = await handler(session, envelope)
                        # else: unhandled type (e.g. a wildcard-bound type
                        # this consumer doesn't act on) — fall through,
                        # commit, ack.

            await message.ack()
            self._record(event_type, "duplicate" if is_duplicate else "processed", started)
            if is_duplicate:
                logger.debug(
                    "duplicate message %s for consumer %s, acked without reprocessing",
                    message_id,
                    self._consumer_name,
                )
            elif self._on_handled is not None and handled_value is not None:
                await self._on_handled(handled_value)
        except Exception:
            logger.exception(
                "handler failed for type=%s message_id=%s consumer=%s",
                event_type,
                message_id,
                self._consumer_name,
            )
            await message.nack(requeue=False)
            self._record(event_type, "dead_lettered", started)

    def _record(self, event_type: str, result: str, started: float) -> None:
        labels = {"consumer": self._consumer_name, "type": event_type}
        _metrics.consumer_messages_total.add(1, {**labels, "result": result})
        _metrics.consumer_processing_duration_seconds.record(time.monotonic() - started, labels)
