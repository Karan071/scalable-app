"""Retry tiers and DLQ metadata (LLD-08 §7).

Opt-in (``InboxConsumer(..., enable_retry_tiers=True)``): every existing
consumer across the workspace keeps today's exact behavior (a handler
failure nacks straight to the queue's own configured DLQ) unless it asks
for this.

Each opted-in queue ``q`` gets four dedicated retry queues (``q.retry.1``
.. ``q.retry.4``, declared in ``deploy/compose/rabbitmq/topology.sh``'s
``queue_with_dlq_and_retries``) -- dedicated, not shared across consumers:
a retry queue's own ``x-dead-letter-routing-key`` is a fixed queue
argument, not a per-message property, so it can only ever point back to
one specific original queue. Each has a fixed TTL and
``x-dead-letter-exchange=""`` with ``x-dead-letter-routing-key=q``, so
once a copy's TTL expires there, RabbitMQ's own dead-letter mechanism
redelivers it straight back to ``q`` with no consumer-side timer or
scheduled job of any kind.

The final DLQ hop is **not** left to RabbitMQ's own automatic dead-
lettering, even though the queue already has a DLX configured
(``queue_with_dlq`` in topology.sh): a plain ``nack`` can't attach the
``x-sf-error-class``/``x-sf-error``/etc. metadata headers LLD-08 §7 wants,
so this publishes the final copy itself, with those headers set, then
acks the original explicitly.
"""

import time
from typing import Literal

import aio_pika
import httpx
from sqlalchemy.exc import DBAPIError

#: Postgres SQLSTATEs sf_db.retry_on_transient also treats as transient
#: (serialization_failure, deadlock_detected) -- duplicated as two
#: constants rather than a new sf-messaging -> sf-db dependency for this
#: alone.
_TRANSIENT_SQLSTATES = frozenset({"40001", "40P01"})

#: (tier, delay_seconds). LLD-08 §7's own table.
RETRY_TIERS: list[tuple[int, float]] = [(1, 5.0), (2, 30.0), (3, 120.0), (4, 600.0)]

ATTEMPT_HEADER = "x-sf-attempt"
FIRST_FAILED_HEADER = "x-sf-first-failed-at"

RouteResult = Literal["retried", "dead_lettered"]


class TransientError(Exception):
    """A handler raises this to mark a failure retry-eligible (LLD-08 §7's
    "transient" class) when it isn't one of the automatically-recognized
    types below -- a business-level transient condition this module has
    no type to recognize on its own (e.g. "the downstream said 503")."""


def is_transient(exc: Exception) -> bool:
    if isinstance(exc, TransientError):
        return True
    if isinstance(exc, httpx.TransportError | httpx.TimeoutException):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (502, 503, 504)
    if isinstance(exc, DBAPIError):
        sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
        return sqlstate in _TRANSIENT_SQLSTATES
    return False


def queue_name_for_tier(queue_name: str, tier: int) -> str:
    return f"{queue_name}.retry.{tier}"


async def route_failure(
    channel: aio_pika.abc.AbstractChannel,
    *,
    queue_name: str,
    message: aio_pika.abc.AbstractIncomingMessage,
    consumer_name: str,
    exc: Exception,
) -> RouteResult:
    """Decides retry-tier vs. DLQ, publishes the copy, and acks the
    original -- the caller never nacks when using this (module
    docstring: the final DLQ hop is an explicit publish, not a nack)."""
    headers = dict(message.headers or {})
    raw_attempt = headers.get(ATTEMPT_HEADER, 0)
    attempt = int(raw_attempt) if isinstance(raw_attempt, int | float | str) else 0
    first_failed_at = str(headers.get(FIRST_FAILED_HEADER) or time.time())

    if is_transient(exc) and attempt < len(RETRY_TIERS):
        tier, _delay_s = RETRY_TIERS[attempt]
        headers[ATTEMPT_HEADER] = attempt + 1
        headers[FIRST_FAILED_HEADER] = first_failed_at
        await channel.default_exchange.publish(
            aio_pika.Message(
                body=message.body,
                headers=headers,
                content_type=message.content_type,
                message_id=message.message_id,
                type=message.type,
            ),
            routing_key=queue_name_for_tier(queue_name, tier),
        )
        await message.ack()
        return "retried"

    headers[ATTEMPT_HEADER] = attempt + 1
    headers[FIRST_FAILED_HEADER] = first_failed_at
    headers["x-sf-error-class"] = "transient" if is_transient(exc) else "permanent"
    headers["x-sf-error"] = f"{type(exc).__name__}: {exc}"[:500]
    headers["x-sf-attempts"] = attempt + 1
    headers["x-sf-consumer"] = consumer_name
    headers["x-sf-original-routing-key"] = message.routing_key or queue_name
    await channel.default_exchange.publish(
        aio_pika.Message(
            body=message.body,
            headers=headers,
            content_type=message.content_type,
            message_id=message.message_id,
            type=message.type,
        ),
        routing_key=f"{queue_name}.dlq",
    )
    await message.ack()
    return "dead_lettered"
