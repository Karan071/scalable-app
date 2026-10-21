"""Lightweight AMQP consumer for identity events the BFF must react to
(LLD-01 §8: disabling a user must revoke all of their sessions within the
target, since Keycloak-side disable alone doesn't touch BFF-held Redis
sessions).

Doesn't reuse ``sf_messaging.InboxConsumer``: that class's dedup is a
Postgres inbox table, and the BFF deliberately has no database (session
state lives in Redis only -- it has no business logic of its own to persist
elsewhere). Dedup here is a Redis ``SET NX EX`` instead, keyed by the
envelope's own id.
"""

import json
import logging
from typing import Any

import aio_pika
from redis.asyncio import Redis

from .sessions import SessionStore

logger = logging.getLogger("bff.event_consumer")

CONSUMER_NAME = "bff.identity"

#: Generous vs. any plausible redelivery window -- just needs to outlive
#: the broker's own retry/backoff, not match any business TTL.
_DEDUP_TTL_S = 86400


class IdentityEventConsumer:
    def __init__(
        self, *, redis: Redis, sessions: SessionStore, amqp_url: str, queue_name: str
    ) -> None:
        self._redis = redis
        self._sessions = sessions
        self._amqp_url = amqp_url
        self._queue_name = queue_name
        self._connection: aio_pika.abc.AbstractRobustConnection | None = None
        self.is_running = False

    async def start(self) -> None:
        self._connection = await aio_pika.connect_robust(self._amqp_url)
        channel = await self._connection.channel()
        await channel.set_qos(prefetch_count=10)
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
            envelope: dict[str, Any] = json.loads(message.body)
            dedup_key = f"bff:event:consumed:{CONSUMER_NAME}:{envelope['id']}"
            is_new = await self._redis.set(dedup_key, "1", nx=True, ex=_DEDUP_TTL_S)
            if not is_new:
                logger.debug(
                    "duplicate identity event %s, acked without reprocessing", envelope["id"]
                )
                await message.ack()
                return

            if envelope.get("type") == "user.disabled":
                user_id = envelope["data"]["user_id"]
                revoked = await self._sessions.delete_all_for_user(user_id)
                logger.info(
                    "user.disabled revoked %d session(s)", revoked, extra={"user_id": user_id}
                )
            # Any other type arriving here is a topology mistake (this
            # queue is only bound to user.disabled.v1) -- ack, don't nack,
            # since redelivery wouldn't change anything.
            await message.ack()
        except Exception:
            logger.exception("identity event handler failed for message_id=%s", message.message_id)
            await message.nack(requeue=False)
