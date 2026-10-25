"""payments-svc data access (LLD-06 §7.1-§7.2, §8.2)."""

from datetime import UTC, datetime
from uuid import UUID, uuid7

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from .models import PaymentAttempt, PaymentEvent, ProviderWebhook, Refund

# --- payment attempts ---------------------------------------------------------


async def get_attempt_by_order(session: AsyncSession, order_id: UUID) -> PaymentAttempt | None:
    stmt = select(PaymentAttempt).where(PaymentAttempt.order_id == order_id)
    return (await session.execute(stmt)).scalars().one_or_none()


async def lock_attempt_by_order(session: AsyncSession, order_id: UUID) -> PaymentAttempt | None:
    stmt = select(PaymentAttempt).where(PaymentAttempt.order_id == order_id).with_for_update()
    return (await session.execute(stmt)).scalars().one_or_none()


async def lock_attempt(session: AsyncSession, attempt_id: UUID) -> PaymentAttempt | None:
    stmt = select(PaymentAttempt).where(PaymentAttempt.id == attempt_id).with_for_update()
    return (await session.execute(stmt)).scalars().one_or_none()


async def get_attempt_by_provider_intent(
    session: AsyncSession, provider_intent_id: str
) -> PaymentAttempt | None:
    stmt = select(PaymentAttempt).where(PaymentAttempt.provider_intent_id == provider_intent_id)
    return (await session.execute(stmt)).scalars().one_or_none()


async def list_unknown_attempts_older_than(
    session: AsyncSession, cutoff: datetime
) -> list[PaymentAttempt]:
    """R2 (LLD-06 §9): attempts the provider never resolved synchronously."""
    stmt = select(PaymentAttempt).where(
        PaymentAttempt.status == "unknown", PaymentAttempt.updated_at < cutoff
    )
    return list((await session.execute(stmt)).scalars().all())


async def insert_attempt(
    session: AsyncSession,
    *,
    org_id: UUID,
    order_id: UUID,
    customer_id: UUID,
    amount_minor: int,
    currency: str,
    pm_token_ref: str,
) -> PaymentAttempt:
    now = datetime.now(UTC)
    attempt = PaymentAttempt(
        id=uuid7(),
        org_id=org_id,
        order_id=order_id,
        customer_id=customer_id,
        amount_minor=amount_minor,
        currency=currency,
        status="authorizing",
        provider="mock",
        provider_intent_id=None,
        provider_sequence=0,
        pm_token_ref=pm_token_ref,
        authorized_at=None,
        captured_at=None,
        last_error_code=None,
        version=1,
        created_at=now,
        updated_at=now,
    )
    session.add(attempt)
    await session.flush()
    return attempt


def mark_status(
    attempt: PaymentAttempt,
    *,
    status: str,
    provider_intent_id: str | None = None,
    provider_sequence: int | None = None,
    last_error_code: str | None = None,
    authorized_at: datetime | None = None,
    captured_at: datetime | None = None,
) -> None:
    attempt.status = status
    if provider_intent_id is not None:
        attempt.provider_intent_id = provider_intent_id
    if provider_sequence is not None:
        attempt.provider_sequence = provider_sequence
    if last_error_code is not None:
        attempt.last_error_code = last_error_code
    if authorized_at is not None:
        attempt.authorized_at = authorized_at
    if captured_at is not None:
        attempt.captured_at = captured_at
    attempt.version += 1
    attempt.updated_at = datetime.now(UTC)


async def insert_event(
    session: AsyncSession,
    *,
    payment_id: UUID,
    event_type: str,
    amount_minor: int,
    provider_event_id: str | None,
) -> None:
    session.add(
        PaymentEvent(
            payment_id=payment_id,
            type=event_type,
            amount_minor=amount_minor,
            provider_event_id=provider_event_id,
            at=datetime.now(UTC),
        )
    )


# --- webhook dedup (LLD-06 §8.2) ---------------------------------------------


async def insert_webhook_record(
    session: AsyncSession, *, provider_event_id: str, signature_valid: bool, payload_sha256: str
) -> bool:
    """Returns ``False`` if this ``provider_event_id`` was already recorded
    (INV-5-style idempotency backstop for duplicate/redelivered webhooks)."""
    stmt = (
        pg_insert(ProviderWebhook)
        .values(
            provider_event_id=provider_event_id,
            received_at=datetime.now(UTC),
            signature_valid=signature_valid,
            payload_sha256=payload_sha256,
            processed_at=None,
            result=None,
        )
        .on_conflict_do_nothing(index_elements=["provider_event_id"])
        .returning(ProviderWebhook.provider_event_id)
    )
    inserted = (await session.execute(stmt)).scalars().one_or_none()
    return inserted is not None


async def mark_webhook_processed(
    session: AsyncSession, provider_event_id: str, result: str
) -> None:
    webhook = await session.get(ProviderWebhook, provider_event_id)
    assert webhook is not None
    webhook.processed_at = datetime.now(UTC)
    webhook.result = result


# --- refunds ------------------------------------------------------------------


async def get_refund_by_payment(session: AsyncSession, payment_id: UUID) -> Refund | None:
    stmt = select(Refund).where(Refund.idempotency_key == payment_id)
    return (await session.execute(stmt)).scalars().one_or_none()


async def insert_refund(session: AsyncSession, *, payment_id: UUID, amount_minor: int) -> Refund:
    """LLD-06 §6: "Full refund in v1" -- at most one refund per payment, so
    ``payment_id`` itself doubles as the idempotency key. orders-svc's
    ``payment.refund_requested.v1`` command carries no separate key of its
    own; this is what makes a redelivery of it safe without needing one."""
    refund = Refund(
        id=uuid7(),
        payment_id=payment_id,
        amount_minor=amount_minor,
        status="refunding",
        idempotency_key=payment_id,
        provider_refund_id=None,
        at=datetime.now(UTC),
    )
    session.add(refund)
    await session.flush()
    return refund


async def mark_refund_completed(
    session: AsyncSession, refund: Refund, *, provider_refund_id: str
) -> None:
    refund.status = "refunded"
    refund.provider_refund_id = provider_refund_id
