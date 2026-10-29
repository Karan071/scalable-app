"""Server-Sent Events: real-time order-status updates (LLD-06 §10).

Fan-out: one shared queue (``q.bff.sse``, any BFF replica) receives
``order.status_changed.v1`` from RabbitMQ and republishes to a redis-cache
pub/sub channel per user (``sse:user:{uid}``) -- whichever replica
actually holds that user's open stream is subscribed to it and writes the
event from there. Doesn't reuse ``sf_messaging.InboxConsumer``: same
reason as ``event_consumer.py``'s own docstring -- that class's dedup
needs a Postgres inbox table, and the BFF deliberately has none. Dedup
here is a Redis ``SET NX EX``, same as ``event_consumer.py``.

``notification.in_app.v1`` (the LLD's other fan-out source, §10 step 1)
isn't consumed: notifications-svc, its only producer, doesn't exist yet
(E4.7's other, larger half -- this pass is order-status SSE only). The
§10 "resume: on reconnect with Last-Event-ID, return the current order
status snapshot" nuance isn't built either -- a reconnect just resumes
the live stream with no replay, which the LLD already says v1 has no
buffer for; the snapshot half is a documented gap, not implemented here.
"""

import json
import logging
from collections.abc import AsyncIterator

import aio_pika
from fastapi import APIRouter, HTTPException, Request
from redis.asyncio import Redis
from sf_common import Bulkhead
from starlette.responses import StreamingResponse

from .request_context import require_session
from .request_context import state as app_state

logger = logging.getLogger("bff.sse")

CONSUMER_NAME = "bff.sse"
#: Generous vs. any plausible redelivery window -- just needs to outlive
#: the broker's own retry/backoff, not match any business TTL.
_DEDUP_TTL_S = 86400
_HEARTBEAT_INTERVAL_S = 15.0
#: LLD-06 §10: "at most 3 streams per user".
MAX_STREAMS_PER_USER = 3

router = APIRouter(prefix="/api")


class SseFanoutConsumer:
    def __init__(self, *, redis: Redis, amqp_url: str, queue_name: str) -> None:
        self._redis = redis
        self._amqp_url = amqp_url
        self._queue_name = queue_name
        self._connection: aio_pika.abc.AbstractRobustConnection | None = None
        self.is_running = False

    async def start(self) -> None:
        self._connection = await aio_pika.connect_robust(self._amqp_url)
        channel = await self._connection.channel()
        await channel.set_qos(prefetch_count=20)
        queue = await channel.declare_queue(self._queue_name, durable=True, passive=True)
        await queue.consume(self._on_message, no_ack=False)
        self.is_running = True

    async def stop(self) -> None:
        self.is_running = False
        if self._connection is not None:
            await self._connection.close()
            self._connection = None

    async def _on_message(self, message: aio_pika.abc.AbstractIncomingMessage) -> None:
        try:
            envelope = json.loads(message.body)
            dedup_key = f"bff:event:consumed:{CONSUMER_NAME}:{envelope['id']}"
            is_new = await self._redis.set(dedup_key, "1", nx=True, ex=_DEDUP_TTL_S)
            if not is_new:
                await message.ack()
                return

            if envelope.get("type") == "order.status_changed":
                await self._publish_order_status_changed(envelope)
            # Any other type arriving here is a topology mistake (this
            # queue is only bound to order.status_changed.v1 today) --
            # ack, don't nack, since redelivery wouldn't change anything.
            await message.ack()
        except Exception:
            logger.exception("sse fan-out handler failed for message_id=%s", message.message_id)
            await message.nack(requeue=False)

    async def _publish_order_status_changed(self, envelope: dict[str, object]) -> None:
        data = envelope["data"]  # type: ignore[assignment]
        payload = json.dumps({"event_id": envelope["id"], "type": "order_status", "data": data})
        await self._redis.publish(f"sse:user:{data['customer_id']}", payload)  # type: ignore[index]


class PerUserStreamLimiter:
    """``sf_common.Bulkhead`` is one fixed counter per instance, not one
    per key -- this wraps a dict of them for the per-user cap instead of
    forcing that shape onto a dynamically-keyed need."""

    def __init__(self, max_per_user: int) -> None:
        self._max = max_per_user
        self._bulkheads: dict[str, Bulkhead] = {}

    def _for(self, uid: str) -> Bulkhead:
        return self._bulkheads.setdefault(uid, Bulkhead(self._max))

    async def try_acquire(self, uid: str) -> bool:
        return await self._for(uid).try_acquire()

    async def release(self, uid: str) -> None:
        await self._for(uid).release()


def _format_event(event_id: str, event_type: str, data: object) -> bytes:
    return f"id: {event_id}\nevent: {event_type}\ndata: {json.dumps(data)}\n\n".encode()


async def _stream(redis: Redis, uid: str, limiter: PerUserStreamLimiter) -> AsyncIterator[bytes]:
    channel = f"sse:user:{uid}"
    pubsub = redis.pubsub()
    await pubsub.subscribe(channel)
    try:
        while True:
            message = await pubsub.get_message(
                ignore_subscribe_messages=True, timeout=_HEARTBEAT_INTERVAL_S
            )
            if message is None:
                yield b": heartbeat\n\n"
                continue
            payload = json.loads(message["data"])
            yield _format_event(payload["event_id"], payload["type"], payload["data"])
    finally:
        await pubsub.unsubscribe(channel)
        await pubsub.aclose()
        await limiter.release(uid)


@router.get("/events")
async def events(request: Request) -> StreamingResponse:
    s = app_state(request)
    _session_id, record = await require_session(request)
    limiter: PerUserStreamLimiter = s.sse_stream_limiter
    if not await limiter.try_acquire(record.uid):
        raise HTTPException(status_code=429, detail="too-many-streams")

    redis: Redis = s.redis_cache
    return StreamingResponse(
        _stream(redis, record.uid, limiter),
        media_type="text/event-stream",
        headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
    )
