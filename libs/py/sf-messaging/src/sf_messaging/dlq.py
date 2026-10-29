"""DLQ inspection and replay/discard (LLD-08 §7.1, E4.5).

``build_dlq_router`` mounts three endpoints per service:

- ``GET /internal/v1/dlq/{queue}?limit=``: peek without consuming.
- ``POST /internal/v1/dlq/{queue}:replay``: republish selected messages
  back to ``{queue}`` with a reset attempt counter.
- ``POST /internal/v1/dlq/{queue}:discard``: permanently remove selected
  messages.

``{queue}`` names the *original* consumer queue (e.g. ``q.inventory.
catalog``), never the literal ``<queue>.dlq`` -- callers shouldn't need to
know that suffix exists.

This module takes no dependency on ``sf_authz``: the caller passes its own
``Internal("dlq:manage")`` (or whatever scope it chooses) as ``auth``, and
its own `Principal`-shaped dependency as ``principal_dependency`` (anything
with a ``.subject`` attribute) so the audit trail and ``x-sf-replayed-by``
carry a real identity -- this module only ever reads ``.subject`` off it,
never imports the type.

**Peek's own limits.** "Get-and-requeue" is exactly that: `basic.get` each
message off the DLQ, then `nack(requeue=True)` what it didn't select. It
is not a snapshot -- a message requeued mid-scan can reappear before the
scan reaches messages behind it, and nothing here prevents a concurrent
reader from doing the same. Fine for a human (or a careful ops script)
looking at a queue nothing else reads from; not a guarantee under
concurrent access.

Every scan stops on the first `basic.get` that returns nothing, not on a
separately-queried message count: a passive queue declare's own count can
read stale (confirmed directly -- immediately after a publish, on a fresh
channel, before `get()` itself proves otherwise) on the very broker this
runs against, so it's never used to decide how many times to loop, only
`_MAX_SCAN` ever bounds that.
"""

import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Annotated, Any, Protocol

import aio_pika
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .audit import record_audit
from .retry import ATTEMPT_HEADER, FIRST_FAILED_HEADER

#: Bounds one scan of a DLQ (peek, replay, or discard) -- large enough for
#: any DLQ this epic expects to see in practice, small enough that a
#: runaway queue can't make one request scan forever.
_MAX_SCAN = 2000


class _HasSubject(Protocol):
    subject: str


#: mypy's structural check on a bare ``Callable[[Request], _HasSubject]``
#: doesn't line up with a concrete ``Callable[[Request], Principal]`` across
#: this module boundary (``Principal`` is frozen; the check isn't). Callers
#: pass whatever Principal-shaped dependency they have -- checked at
#: runtime by `.subject` access, not statically here.
_PrincipalDependency = Callable[[Request], Any]


class DlqMessageOut(BaseModel):
    message_id: str | None
    attempts: int | None
    error_class: str | None
    error: str | None
    first_failed_at: str | None
    consumer: str | None
    original_routing_key: str | None
    body: Any


class ReplayRequest(BaseModel):
    message_ids: list[str] | None = None
    all: bool = False
    reason: str


class DiscardRequest(BaseModel):
    message_ids: list[str]
    reason: str


def _dlq_name(queue: str, known_queues: Sequence[str]) -> str:
    if queue not in known_queues:
        raise HTTPException(status_code=404, detail=f"unknown queue: {queue}")
    return f"{queue}.dlq"


def _to_out(message: aio_pika.abc.AbstractIncomingMessage) -> DlqMessageOut:
    headers = message.headers or {}
    attempts = headers.get("x-sf-attempts")
    return DlqMessageOut(
        message_id=message.message_id,
        attempts=int(attempts) if isinstance(attempts, int | float | str) else None,
        error_class=_str_or_none(headers.get("x-sf-error-class")),
        error=_str_or_none(headers.get("x-sf-error")),
        first_failed_at=_str_or_none(headers.get(FIRST_FAILED_HEADER)),
        consumer=_str_or_none(headers.get("x-sf-consumer")),
        original_routing_key=_str_or_none(headers.get("x-sf-original-routing-key")),
        body=_decode_body(message.body),
    )


def _str_or_none(value: object) -> str | None:
    return str(value) if value is not None else None


def _decode_body(body: bytes) -> Any:
    try:
        return json.loads(body)
    except ValueError:
        return None


async def _scan_all(
    channel: aio_pika.abc.AbstractChannel, dlq: str, *, limit: int = _MAX_SCAN
) -> list[aio_pika.abc.AbstractIncomingMessage]:
    """Fetches up to ``limit`` messages currently on ``dlq``, unacked, and
    returns them -- the caller decides what happens to each one *after*
    this returns, never interleaved with another `get()` here.

    That ordering matters, not just for tidiness: `nack(requeue=True)` on
    a message this same scan hasn't reached everything behind yet can make
    RabbitMQ redeliver it before the rest -- confirmed directly (a
    discard of message 2 of 2 never found it, because nacking message 1
    as "not selected" put it right back in front of 2). Finishing the
    fetch first, then nacking the ones a caller doesn't want, removes that
    hazard: nothing calls `get()` again after any `nack()` runs.
    """
    queue = await channel.get_queue(dlq)
    got: list[aio_pika.abc.AbstractIncomingMessage] = []
    for _ in range(limit):
        message = await queue.get(no_ack=False, fail=False)
        if message is None:
            break
        got.append(message)
    return got


def build_dlq_router(
    *,
    service_name: str,
    queue_names: Sequence[str],
    amqp_url: str,
    session_factory: async_sessionmaker[AsyncSession],
    notify_channel: str,
    auth: Callable[..., Any],
    principal_dependency: _PrincipalDependency,
) -> APIRouter:
    router = APIRouter(prefix="/internal/v1/dlq", tags=["dlq"], dependencies=[Depends(auth)])
    Principal = Annotated[_HasSubject, Depends(principal_dependency)]

    @router.get("/{queue}", response_model=list[DlqMessageOut])
    async def peek(queue: str, limit: int = 20) -> list[DlqMessageOut]:
        dlq = _dlq_name(queue, queue_names)
        limit = max(0, min(limit, _MAX_SCAN))
        connection = await aio_pika.connect_robust(amqp_url)
        try:
            channel = await connection.channel()
            got = await _scan_all(channel, dlq, limit=limit)
            out = [_to_out(m) for m in got]
            for message in got:
                await message.nack(requeue=True)
            return out
        finally:
            await connection.close()

    async def _record_dlq_audit(
        *, action: str, principal: _HasSubject, queue: str, reason: str, count: int
    ) -> None:
        async with session_factory() as session, session.begin():
            await record_audit(
                session,
                notify_channel=notify_channel,
                source_service=service_name,
                action=action,
                actor_type="service",
                actor_id=principal.subject,
                domain="platform",
                target_type="dlq_queue",
                target_id=queue,
                aggregate_version=1,
                reason=reason,
                changes={"count": count},
            )

    @router.post("/{queue}:replay")
    async def replay(queue: str, body: ReplayRequest, principal: Principal) -> dict[str, int]:
        if not body.all and not body.message_ids:
            raise HTTPException(422, detail="message_ids or all=true is required")
        wanted = set(body.message_ids or [])

        dlq = _dlq_name(queue, queue_names)
        connection = await aio_pika.connect_robust(amqp_url)
        try:
            channel = await connection.channel()
            got = await _scan_all(channel, dlq)
            replayed = 0
            for message in got:
                if not (body.all or message.message_id in wanted):
                    await message.nack(requeue=True)
                    continue
                headers = dict(message.headers or {})
                headers[ATTEMPT_HEADER] = 0
                headers[FIRST_FAILED_HEADER] = datetime.now(UTC).isoformat()
                headers["x-sf-replayed-by"] = principal.subject
                await channel.default_exchange.publish(
                    aio_pika.Message(
                        body=message.body,
                        headers=headers,
                        content_type=message.content_type,
                        message_id=message.message_id,
                        type=message.type,
                    ),
                    routing_key=queue,
                )
                await message.ack()
                replayed += 1
        finally:
            await connection.close()

        if replayed:
            await _record_dlq_audit(
                action="dlq.replayed",
                principal=principal,
                queue=queue,
                reason=body.reason,
                count=replayed,
            )
        return {"replayed": replayed}

    @router.post("/{queue}:discard")
    async def discard(queue: str, body: DiscardRequest, principal: Principal) -> dict[str, int]:
        wanted = set(body.message_ids)
        dlq = _dlq_name(queue, queue_names)
        connection = await aio_pika.connect_robust(amqp_url)
        try:
            channel = await connection.channel()
            got = await _scan_all(channel, dlq)
            discarded = 0
            for message in got:
                if message.message_id in wanted:
                    await message.ack()
                    discarded += 1
                else:
                    await message.nack(requeue=True)
        finally:
            await connection.close()

        if discarded:
            await _record_dlq_audit(
                action="dlq.discarded",
                principal=principal,
                queue=queue,
                reason=body.reason,
                count=discarded,
            )
        return {"discarded": discarded}

    return router
