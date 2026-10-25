"""payments-svc business logic (LLD-06 §7-§9)."""

import hashlib
import hmac
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
from sf_db import transaction
from sf_messaging import add_to_outbox, build_envelope
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from . import repository
from .models import PaymentAttempt
from .provider_client import (
    AuthorizationExpired,
    CaptureFailed,
    MockProviderClient,
    ProviderDeclined,
    ProviderIntentNotFound,
)

_NOTIFY_CHANNEL = "outbox_payments"
_EVENTS_EXCHANGE = "sf.events"

_STATUS_CODES = {
    "authorizing": 202,
    "unknown": 202,
    "authorized": 200,
    "capturing": 200,
    "captured": 200,
    "declined": 402,
    "voided": 200,
    "refunding": 200,
    "refunded": 200,
}


class SignatureInvalid(Exception):
    pass


class TimestampStale(Exception):
    pass


class BodyTooLarge(Exception):
    pass


@dataclass(frozen=True)
class AuthorizeOutcome:
    status_code: int
    payment_id: UUID
    status: str


async def _emit(
    session: AsyncSession,
    *,
    event_type: str,
    routing_key: str,
    aggregate_id: str,
    aggregate_version: int,
    org_id: UUID,
    data: dict[str, Any],
) -> None:
    envelope = build_envelope(
        event_type=event_type,
        source="payments-svc",
        subject=aggregate_id,
        data=data,
        schema_version="1.0",
        aggregate_type="payment_attempt",
        aggregate_version=aggregate_version,
        org_id=str(org_id),
    )
    await add_to_outbox(
        session,
        envelope,
        exchange=_EVENTS_EXCHANGE,
        routing_key=routing_key,
        aggregate_id=aggregate_id,
        notify_channel=_NOTIFY_CHANNEL,
    )


def _summary(attempt: PaymentAttempt) -> dict[str, Any]:
    return {
        "payment_id": str(attempt.id),
        "order_id": str(attempt.order_id),
        "org_id": str(attempt.org_id),
        "status": attempt.status,
        "amount_minor": attempt.amount_minor,
        "currency": attempt.currency,
    }


# --- authorize (LLD-06 §7.2, §5.1) ------------------------------------------


async def authorize(
    session_factory: async_sessionmaker[AsyncSession],
    provider: MockProviderClient,
    *,
    order_id: UUID,
    org_id: UUID,
    customer_id: UUID,
    amount_minor: int,
    currency: str,
    payment_method_token: str,
) -> AuthorizeOutcome:
    async with transaction(session_factory) as session:
        attempt = await repository.lock_attempt_by_order(session, order_id)
        if attempt is None:
            attempt = await repository.insert_attempt(
                session,
                org_id=org_id,
                order_id=order_id,
                customer_id=customer_id,
                amount_minor=amount_minor,
                currency=currency,
                pm_token_ref=payment_method_token,
            )

    if attempt.status not in ("authorizing", "unknown"):
        # A real replay (idempotent by order_id) -- nothing left to do.
        return AuthorizeOutcome(
            status_code=_STATUS_CODES[attempt.status], payment_id=attempt.id, status=attempt.status
        )

    # Still unresolved (fresh, or a resumed "unknown"): (re)attempt the
    # provider call. Idempotent by attempt.id, so retrying a still-open
    # attempt is always safe, whether this is the first try or a client
    # retry resuming a crashed one.
    idempotency_key = f"{attempt.id}:authorize"
    try:
        intent = await provider.authorize(
            idempotency_key=idempotency_key,
            amount_minor=amount_minor,
            currency=currency,
            payment_method_token=payment_method_token,
            metadata={"attempt_id": str(attempt.id)},
        )
    except ProviderDeclined:
        async with transaction(session_factory) as session:
            locked = await repository.lock_attempt(session, attempt.id)
            assert locked is not None
            repository.mark_status(locked, status="declined", last_error_code="card_declined")
            await repository.insert_event(
                session,
                payment_id=locked.id,
                event_type="declined",
                amount_minor=amount_minor,
                provider_event_id=None,
            )
            await _emit(
                session,
                event_type="payment.failed",
                routing_key="payment.failed.v1",
                aggregate_id=str(locked.id),
                aggregate_version=locked.version,
                org_id=org_id,
                data=_summary(locked),
            )
        return AuthorizeOutcome(status_code=402, payment_id=attempt.id, status="declined")
    except (httpx.TimeoutException, httpx.TransportError):
        # LLD-06 §7.2: "unknown". Resolved later by a webhook or R2.
        async with transaction(session_factory) as session:
            locked = await repository.lock_attempt(session, attempt.id)
            assert locked is not None
            repository.mark_status(locked, status="unknown")
        return AuthorizeOutcome(status_code=202, payment_id=attempt.id, status="unknown")

    async with transaction(session_factory) as session:
        locked = await repository.lock_attempt(session, attempt.id)
        assert locked is not None
        repository.mark_status(
            locked,
            status="authorized",
            provider_intent_id=intent.id,
            provider_sequence=intent.sequence,
            authorized_at=datetime.now(UTC),
        )
        await repository.insert_event(
            session,
            payment_id=locked.id,
            event_type="authorized",
            amount_minor=amount_minor,
            provider_event_id=None,
        )
        await _emit(
            session,
            event_type="payment.authorized",
            routing_key="payment.authorized.v1",
            aggregate_id=str(locked.id),
            aggregate_version=locked.version,
            org_id=org_id,
            data=_summary(locked),
        )
    return AuthorizeOutcome(status_code=200, payment_id=attempt.id, status="authorized")


# --- commands consumed (LLD-06 §5.1, §6) ------------------------------------


async def handle_capture_requested(
    session_factory: async_sessionmaker[AsyncSession],
    provider: MockProviderClient,
    *,
    order_id: UUID,
) -> None:
    async with transaction(session_factory) as session:
        attempt = await repository.lock_attempt_by_order(session, order_id)
        if attempt is None or attempt.status != "authorized":
            return  # already captured, or nothing to capture -- idempotent no-op
        repository.mark_status(attempt, status="capturing")
        attempt_id, provider_intent_id, org_id, amount_minor = (
            attempt.id,
            attempt.provider_intent_id,
            attempt.org_id,
            attempt.amount_minor,
        )
    assert provider_intent_id is not None

    idempotency_key = f"{attempt_id}:capture"
    try:
        intent = await provider.capture(
            idempotency_key=idempotency_key, provider_intent_id=provider_intent_id
        )
    except (AuthorizationExpired, CaptureFailed, ProviderIntentNotFound) as exc:
        error_code = (
            "authorization_expired" if isinstance(exc, AuthorizationExpired) else "capture_failed"
        )
        # LLD-06 §7.2: "capturing -> authorized: capture failed
        # (retryable)". Simplified (see LLD-06's "As built"): treated as
        # terminal on the first failure here too, symmetric with
        # orders-svc's own simplification of the 5x retry.
        async with transaction(session_factory) as session:
            locked = await repository.lock_attempt(session, attempt_id)
            assert locked is not None
            repository.mark_status(locked, status="authorized", last_error_code=error_code)
            await repository.insert_event(
                session,
                payment_id=locked.id,
                event_type="capture_failed",
                amount_minor=amount_minor,
                provider_event_id=None,
            )
            await _emit(
                session,
                event_type="payment.failed",
                routing_key="payment.failed.v1",
                aggregate_id=str(locked.id),
                aggregate_version=locked.version,
                org_id=org_id,
                data=_summary(locked),
            )
        return

    async with transaction(session_factory) as session:
        locked = await repository.lock_attempt(session, attempt_id)
        assert locked is not None
        repository.mark_status(
            locked,
            status="captured",
            provider_sequence=intent.sequence,
            captured_at=datetime.now(UTC),
        )
        await repository.insert_event(
            session,
            payment_id=locked.id,
            event_type="captured",
            amount_minor=amount_minor,
            provider_event_id=None,
        )
        await _emit(
            session,
            event_type="payment.succeeded",
            routing_key="payment.succeeded.v1",
            aggregate_id=str(locked.id),
            aggregate_version=locked.version,
            org_id=org_id,
            data=_summary(locked),
        )


async def handle_void_requested(
    session_factory: async_sessionmaker[AsyncSession],
    provider: MockProviderClient,
    *,
    order_id: UUID,
) -> None:
    async with transaction(session_factory) as session:
        attempt = await repository.lock_attempt_by_order(session, order_id)
        if attempt is None or attempt.status != "authorized":
            return  # already voided/captured/declined, or nothing to void
        attempt_id, provider_intent_id, org_id = (
            attempt.id,
            attempt.provider_intent_id,
            attempt.org_id,
        )
    assert provider_intent_id is not None

    idempotency_key = f"{attempt_id}:void"
    intent = await provider.void(
        idempotency_key=idempotency_key, provider_intent_id=provider_intent_id
    )

    async with transaction(session_factory) as session:
        locked = await repository.lock_attempt(session, attempt_id)
        assert locked is not None
        repository.mark_status(locked, status="voided", provider_sequence=intent.sequence)
        await repository.insert_event(
            session,
            payment_id=locked.id,
            event_type="voided",
            amount_minor=locked.amount_minor,
            provider_event_id=None,
        )
        await _emit(
            session,
            event_type="payment.voided",
            routing_key="payment.voided.v1",
            aggregate_id=str(locked.id),
            aggregate_version=locked.version,
            org_id=org_id,
            data=_summary(locked),
        )


async def handle_refund_requested(
    session_factory: async_sessionmaker[AsyncSession],
    provider: MockProviderClient,
    *,
    order_id: UUID,
) -> None:
    async with transaction(session_factory) as session:
        attempt = await repository.lock_attempt_by_order(session, order_id)
        if attempt is None or attempt.status != "captured":
            return  # nothing captured to refund -- idempotent no-op (LLD-06 §6)
        existing_refund = await repository.get_refund_by_payment(session, attempt.id)
        if existing_refund is not None and existing_refund.status == "refunded":
            return  # already completed
        attempt_id, provider_intent_id, org_id, amount_minor = (
            attempt.id,
            attempt.provider_intent_id,
            attempt.org_id,
            attempt.amount_minor,
        )
        if existing_refund is None:
            await repository.insert_refund(
                session, payment_id=attempt_id, amount_minor=amount_minor
            )
    assert provider_intent_id is not None

    idempotency_key = f"{attempt_id}:refund"
    refund = await provider.refund(
        idempotency_key=idempotency_key,
        provider_intent_id=provider_intent_id,
        amount_minor=amount_minor,
    )

    async with transaction(session_factory) as session:
        locked = await repository.lock_attempt(session, attempt_id)
        assert locked is not None
        refund_row = await repository.get_refund_by_payment(session, attempt_id)
        assert refund_row is not None
        await repository.mark_refund_completed(session, refund_row, provider_refund_id=refund.id)
        repository.mark_status(locked, status="refunded")
        await repository.insert_event(
            session,
            payment_id=locked.id,
            event_type="refunded",
            amount_minor=amount_minor,
            provider_event_id=None,
        )
        await _emit(
            session,
            event_type="payment.refunded",
            routing_key="payment.refunded.v1",
            aggregate_id=str(locked.id),
            aggregate_version=locked.version,
            org_id=org_id,
            data=_summary(locked),
        )


# --- webhooks (LLD-06 §8.2) -------------------------------------------------

_EVENT_TRANSITIONS: dict[str, tuple[str, str, str]] = {
    # provider event type -> (new status, event ledger type, routing key)
    "payment_intent.authorized": ("authorized", "authorized", "payment.authorized.v1"),
    "payment_intent.captured": ("captured", "captured", "payment.succeeded.v1"),
    "payment_intent.voided": ("voided", "voided", "payment.voided.v1"),
    "payment_intent.refunded": ("refunded", "refunded", "payment.refunded.v1"),
}


def verify_signature(
    *, secrets: list[str], signature_header: str, body: bytes, clock_skew_tolerance_s: int
) -> None:
    """LLD-06 §8.2 steps 2-3. Raises on any failure; never partially trusts
    a signature."""
    parts = dict(part.split("=", 1) for part in signature_header.split(",") if "=" in part)
    try:
        timestamp = int(parts["t"])
        candidate = parts["v1"]
    except (KeyError, ValueError) as exc:
        raise SignatureInvalid("malformed SF-Signature header") from exc

    if abs(time.time() - timestamp) > clock_skew_tolerance_s:
        raise TimestampStale(timestamp)

    payload = f"{timestamp}.".encode() + body
    if not any(
        hmac.compare_digest(
            candidate, hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        )
        for secret in secrets
    ):
        raise SignatureInvalid("signature mismatch")


async def process_webhook(
    session_factory: async_sessionmaker[AsyncSession], *, event: dict[str, Any], payload_sha256: str
) -> str:
    """Returns ``"duplicate"`` or ``"processed"``/``"ignored"`` (LLD-06
    §8.2 steps 4-6). Caller has already verified the signature."""
    provider_event_id = event["provider_event_id"]
    async with transaction(session_factory) as session:
        inserted = await repository.insert_webhook_record(
            session,
            provider_event_id=provider_event_id,
            signature_valid=True,
            payload_sha256=payload_sha256,
        )
        if not inserted:
            return "duplicate"

        obj = event["data"]["object"]
        provider_intent_id = obj["id"]
        sequence = obj["sequence"]
        event_type = event["type"]

        attempt = await repository.get_attempt_by_provider_intent(session, provider_intent_id)
        if attempt is None:
            # The race LLD-06 §7.2's "unknown" exists for: the synchronous
            # authorize call never got a provider_intent_id back, but the
            # provider echoes our own attempt_id in metadata.
            attempt_id = obj.get("metadata", {}).get("attempt_id")
            if attempt_id is not None:
                attempt = await repository.lock_attempt(session, UUID(attempt_id))
                if attempt is not None and attempt.provider_intent_id is None:
                    repository.mark_status(
                        attempt, status=attempt.status, provider_intent_id=provider_intent_id
                    )
        if attempt is None:
            await repository.mark_webhook_processed(session, provider_event_id, "unmatched")
            return "ignored"

        locked = await repository.lock_attempt(session, attempt.id)
        assert locked is not None
        transition = _EVENT_TRANSITIONS.get(event_type)
        if transition is None or sequence <= locked.provider_sequence:
            await repository.mark_webhook_processed(session, provider_event_id, "stale")
            return "ignored"

        new_status, ledger_type, routing_key = transition
        now = datetime.now(UTC)
        repository.mark_status(
            locked,
            status=new_status,
            provider_sequence=sequence,
            authorized_at=now if locked.authorized_at is None else None,
            captured_at=now if new_status == "captured" else None,
        )
        await repository.insert_event(
            session,
            payment_id=locked.id,
            event_type=ledger_type,
            amount_minor=locked.amount_minor,
            provider_event_id=provider_event_id,
        )
        await _emit(
            session,
            event_type=routing_key.removesuffix(".v1"),
            routing_key=routing_key,
            aggregate_id=str(locked.id),
            aggregate_version=locked.version,
            org_id=locked.org_id,
            data=_summary(locked),
        )
        await repository.mark_webhook_processed(session, provider_event_id, "processed")
        return "processed"


# --- R2 reconciliation: unknown payments (LLD-06 §9) ------------------------


async def reconcile_unknown_attempts(
    session_factory: async_sessionmaker[AsyncSession],
    provider: MockProviderClient,
    *,
    older_than_s: float,
) -> int:
    """R2: "unknown > 30s -> GET provider intent -> resolve". Retries the
    *same* idempotent authorize call rather than a bare status GET --
    this mock (like most real PSPs) has no "look up by idempotency key"
    endpoint, so replaying the original request is what actually
    resolves an attempt that never got a `provider_intent_id` back."""
    cutoff = datetime.now(UTC) - timedelta(seconds=older_than_s)
    async with transaction(session_factory) as session:
        stale = await repository.list_unknown_attempts_older_than(session, cutoff)
    resolved = 0
    for attempt in stale:
        outcome = await authorize(
            session_factory,
            provider,
            order_id=attempt.order_id,
            org_id=attempt.org_id,
            customer_id=attempt.customer_id,
            amount_minor=attempt.amount_minor,
            currency=attempt.currency,
            payment_method_token=attempt.pm_token_ref,
        )
        if outcome.status != "unknown":
            resolved += 1
    return resolved
