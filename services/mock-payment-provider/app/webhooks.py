"""Signed webhook delivery (ADR-0008, LLD-06 §8.2).

A background loop polls the store for due webhooks and POSTs each to
``settings.webhook_target_url`` (payments-svc), signed the same way a
real PSP would sign it -- so payments-svc's signature verification is
exercised for real, not assumed.
"""

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import time

import httpx

from app.store import PaymentIntent, PendingWebhook, Store

logger = logging.getLogger("mock_payment_provider.webhooks")

_POLL_INTERVAL_S = 0.5


def sign(secret: str, *, timestamp: int, body: bytes) -> str:
    payload = f"{timestamp}.".encode() + body
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def _event_payload(webhook: PendingWebhook, intent: PaymentIntent) -> dict:
    return {
        "provider_event_id": f"{webhook.intent_id}:{webhook.event_type}:{webhook.sequence}",
        "type": webhook.event_type,
        "created": int(time.time()),
        "data": {
            "object": {
                "id": intent.id,
                "object": "payment_intent",
                "status": intent.status,
                "amount_minor": intent.amount_minor,
                "currency": intent.currency,
                "sequence": webhook.sequence,
                "metadata": intent.metadata,
            }
        },
    }


class WebhookSender:
    def __init__(
        self, *, store: Store, http_client: httpx.AsyncClient, target_url: str, secret: str
    ) -> None:
        self._store = store
        self._http = http_client
        self._target_url = target_url
        self._secret = secret
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
            await asyncio.sleep(_POLL_INTERVAL_S)
            try:
                for webhook in self._store.due_webhooks():
                    await self.deliver(webhook)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("webhook delivery tick failed")

    async def deliver(self, webhook: PendingWebhook) -> None:
        intent = self._store.intents.get(webhook.intent_id)
        if intent is None:
            return
        body = json.dumps(_event_payload(webhook, intent)).encode()
        timestamp = int(time.time())
        signature = sign(self._secret, timestamp=timestamp, body=body)
        try:
            await self._http.post(
                self._target_url,
                content=body,
                headers={
                    "content-type": "application/json",
                    "sf-signature": f"t={timestamp},v1={signature}",
                },
            )
        except httpx.HTTPError:
            logger.exception(
                "webhook delivery failed for %s %s", webhook.intent_id, webhook.event_type
            )
