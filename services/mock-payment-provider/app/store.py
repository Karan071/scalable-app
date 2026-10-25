"""In-memory payment-intent store and scenario logic (LLD-06 §8.1, ADR-0008).

No database: this is a disposable sandbox, reset between test runs via
``POST /__mock/reset``. State lives only as long as the process does --
exactly what a mock PSP needs to be, nothing more.
"""

import time
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import uuid4

Status = Literal["authorized", "processing", "captured", "voided", "declined", "refunded"]

#: LLD-06 §8.1. Any token not listed here behaves like `pm_mock_success`.
_DECLINE_TOKEN = "pm_mock_decline"
_TIMEOUT_TOKEN = "pm_mock_timeout"
_DELAYED_WEBHOOK_TOKEN = "pm_mock_delayed_webhook"
_DUPLICATE_WEBHOOK_TOKEN = "pm_mock_duplicate_webhook"
_OUT_OF_ORDER_TOKEN = "pm_mock_out_of_order"
_CAPTURE_FAIL_TOKEN = "pm_mock_capture_fail"
_AUTH_EXPIRES_TOKEN = "pm_mock_auth_expires"


class CardDeclined(Exception):
    pass


class IntentNotFound(Exception):
    pass


class AuthorizationExpired(Exception):
    pass


class CaptureFailed(Exception):
    pass


@dataclass
class PendingWebhook:
    intent_id: str
    event_type: str
    sequence: int
    fire_at: float
    sent: bool = False


@dataclass
class PaymentIntent:
    id: str
    amount_minor: int
    currency: str
    payment_method_token: str
    status: Status
    sequence: int = 0
    capture_attempts: int = 0
    always_fail_capture: bool = False
    expires_at: float | None = None
    #: Echoed back in every webhook payload (real PSPs support this, e.g.
    #: Stripe's `metadata`) -- how payments-svc correlates a webhook back
    #: to its own attempt when it never got a synchronous
    #: `provider_intent_id` (the authorize call timed out, LLD-06 §7.2's
    #: "unknown" state).
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass
class Refund:
    id: str
    payment_intent_id: str
    amount_minor: int
    status: str = "refunded"


@dataclass
class Store:
    intents: dict[str, PaymentIntent] = field(default_factory=dict)
    refunds: dict[str, Refund] = field(default_factory=dict)
    idempotency: dict[str, tuple[int, dict[str, Any]]] = field(default_factory=dict)
    pending_webhooks: list[PendingWebhook] = field(default_factory=list)

    def reset(self) -> None:
        self.intents.clear()
        self.refunds.clear()
        self.idempotency.clear()
        self.pending_webhooks.clear()

    def _schedule(
        self,
        intent: PaymentIntent,
        event_type: str,
        delay_s: float,
        *,
        copies: int = 1,
        sequence: int | None = None,
    ) -> None:
        fire_at = time.monotonic() + delay_s
        for _ in range(copies):
            self.pending_webhooks.append(
                PendingWebhook(
                    intent_id=intent.id,
                    event_type=event_type,
                    sequence=intent.sequence if sequence is None else sequence,
                    fire_at=fire_at,
                )
            )

    def create_intent(
        self,
        *,
        amount_minor: int,
        currency: str,
        payment_method_token: str,
        delay_scale: float,
        metadata: dict[str, str] | None = None,
    ) -> PaymentIntent:
        intent = PaymentIntent(
            id=str(uuid4()),
            amount_minor=amount_minor,
            currency=currency,
            payment_method_token=payment_method_token,
            status="processing",
            metadata=metadata or {},
        )
        self.intents[intent.id] = intent

        if payment_method_token == _DECLINE_TOKEN:
            intent.status = "declined"
            intent.sequence += 1
            raise CardDeclined(intent.id)

        if payment_method_token == _AUTH_EXPIRES_TOKEN:
            intent.status = "authorized"
            intent.sequence += 1
            intent.expires_at = time.monotonic() + 120 * delay_scale
            self._schedule(intent, "payment_intent.authorized", 1 * delay_scale)
            return intent

        if payment_method_token == _CAPTURE_FAIL_TOKEN:
            intent.status = "authorized"
            intent.sequence += 1
            self._schedule(intent, "payment_intent.authorized", 1 * delay_scale)
            return intent

        if payment_method_token == _OUT_OF_ORDER_TOKEN:
            # Authorize and capture happen back to back; the CAPTURED
            # webhook is scheduled to *fire* before the AUTHORIZED one,
            # but still carries the higher sequence number (2 vs. 1) --
            # authorization logically still happened first, only its
            # webhook is delayed. This is what actually exercises the
            # consumer's out-of-order guard (sequence, not arrival order).
            intent.status = "captured"
            intent.sequence = 2
            self._schedule(intent, "payment_intent.captured", 1 * delay_scale, sequence=2)
            self._schedule(intent, "payment_intent.authorized", 2 * delay_scale, sequence=1)
            return intent

        if payment_method_token == _DUPLICATE_WEBHOOK_TOKEN:
            intent.status = "authorized"
            intent.sequence += 1
            self._schedule(intent, "payment_intent.authorized", 1 * delay_scale, copies=3)
            return intent

        if payment_method_token == _DELAYED_WEBHOOK_TOKEN:
            # Stays "processing" in the sync response; the webhook (at
            # +60s) is what actually authorizes it.
            self._schedule(intent, "payment_intent.authorized", 60 * delay_scale)
            return intent

        if payment_method_token == _TIMEOUT_TOKEN:
            # The caller's own client times out well before this returns
            # (LLD-06 §8.1: "sleeps 10s, beyond the 5s client timeout").
            intent.status = "authorized"
            intent.sequence += 1
            self._schedule(intent, "payment_intent.authorized", 15 * delay_scale)
            return intent

        # pm_mock_success, or any other token.
        intent.status = "authorized"
        intent.sequence += 1
        self._schedule(intent, "payment_intent.authorized", 1 * delay_scale)
        return intent

    def get_intent(self, intent_id: str) -> PaymentIntent:
        intent = self.intents.get(intent_id)
        if intent is None:
            raise IntentNotFound(intent_id)
        return intent

    def capture(self, intent_id: str, *, delay_scale: float) -> PaymentIntent:
        intent = self.get_intent(intent_id)
        if intent.expires_at is not None and time.monotonic() > intent.expires_at:
            raise AuthorizationExpired(intent_id)
        if intent.status == "captured":
            return intent  # idempotent replay
        if intent.payment_method_token == _CAPTURE_FAIL_TOKEN:
            intent.capture_attempts += 1
            if intent.always_fail_capture or intent.capture_attempts <= 2:
                raise CaptureFailed(intent_id)
        intent.status = "captured"
        intent.sequence += 1
        self._schedule(intent, "payment_intent.captured", 1 * delay_scale)
        return intent

    def void(self, intent_id: str, *, delay_scale: float) -> PaymentIntent:
        intent = self.get_intent(intent_id)
        if intent.status == "voided":
            return intent
        intent.status = "voided"
        intent.sequence += 1
        self._schedule(intent, "payment_intent.voided", 1 * delay_scale)
        return intent

    def refund(self, intent_id: str, *, amount_minor: int, delay_scale: float) -> Refund:
        intent = self.get_intent(intent_id)
        existing = next(
            (r for r in self.refunds.values() if r.payment_intent_id == intent_id), None
        )
        if existing is not None:
            return existing  # idempotent replay
        refund = Refund(id=str(uuid4()), payment_intent_id=intent_id, amount_minor=amount_minor)
        self.refunds[refund.id] = refund
        intent.sequence += 1
        self._schedule(intent, "payment_intent.refunded", 1 * delay_scale)
        return refund

    def due_webhooks(self) -> list[PendingWebhook]:
        now = time.monotonic()
        due = [w for w in self.pending_webhooks if not w.sent and w.fire_at <= now]
        for w in due:
            w.sent = True
        return due

    def force_webhook(self, intent_id: str, event_type: str) -> PendingWebhook:
        """``/__mock/...`` control API: fire a specific pending webhook now,
        for tests that would otherwise wait out a real 15s/60s delay."""
        for w in self.pending_webhooks:
            if w.intent_id == intent_id and w.event_type == event_type and not w.sent:
                w.sent = True
                w.fire_at = time.monotonic()
                return w
        raise IntentNotFound(f"no pending {event_type} for {intent_id}")
