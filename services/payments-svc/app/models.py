"""SQLAlchemy models for the ``payments`` schema (LLD-06 §7.1) and its
outbox (LLD-08 §5.1).
"""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    # Every migration column is timestamptz (LLD-13 §6: UTC everywhere); see
    # users-svc/app/models.py for why this mapping is needed.
    type_annotation_map = {datetime: DateTime(timezone=True)}


#: LLD-06 §7.2's payment state machine, plus "unknown" (provider timeout,
#: resolved by R2 or a webhook) and "authorizing"/"refunding" as the
#: in-flight state before each terminal-ish outcome.
PAYMENT_STATUSES = (
    "authorizing",
    "authorized",
    "declined",
    "unknown",
    "capturing",
    "captured",
    "voided",
    "refunding",
    "refunded",
)


class PaymentAttempt(Base):
    __tablename__ = "payment_attempts"
    __table_args__ = (
        CheckConstraint(f"status IN {PAYMENT_STATUSES!r}", name="ck_payment_attempts_status"),
        UniqueConstraint("provider_intent_id", name="uq_payment_attempts_provider_intent"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    org_id: Mapped[uuid.UUID]
    order_id: Mapped[uuid.UUID]
    customer_id: Mapped[uuid.UUID]
    amount_minor: Mapped[int]
    currency: Mapped[str]
    status: Mapped[str]
    provider: Mapped[str]
    provider_intent_id: Mapped[str | None]
    #: The out-of-order guard (LLD-06 §8.2 step 5): a webhook/poll result
    #: is only applied if its sequence is greater than this.
    provider_sequence: Mapped[int]
    pm_token_ref: Mapped[str]
    authorized_at: Mapped[datetime | None]
    captured_at: Mapped[datetime | None]
    last_error_code: Mapped[str | None]
    version: Mapped[int]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class PaymentEvent(Base):
    """Append-only ledger (LLD-06 §7.1)."""

    __tablename__ = "payment_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    payment_id: Mapped[uuid.UUID]
    type: Mapped[str]
    amount_minor: Mapped[int]
    provider_event_id: Mapped[str | None]
    at: Mapped[datetime]


class ProviderWebhook(Base):
    """LLD-06 §8.2 step 4: the dedup table. ``provider_event_id`` is the
    whole idempotency guarantee -- a redelivered or duplicated webhook
    (``pm_mock_duplicate_webhook``) hits this PK and is a 200 no-op."""

    __tablename__ = "provider_webhooks"

    provider_event_id: Mapped[str] = mapped_column(primary_key=True)
    received_at: Mapped[datetime]
    signature_valid: Mapped[bool]
    payload_sha256: Mapped[str]
    processed_at: Mapped[datetime | None]
    result: Mapped[str | None]


class Refund(Base):
    __tablename__ = "refunds"
    __table_args__ = (UniqueConstraint("idempotency_key", name="uq_refunds_idempotency_key"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    payment_id: Mapped[uuid.UUID]
    amount_minor: Mapped[int]
    status: Mapped[str]
    idempotency_key: Mapped[uuid.UUID]
    provider_refund_id: Mapped[str | None]
    at: Mapped[datetime]


class Outbox(Base):
    """LLD-08 §5.1. The partial index on unpublished rows is DDL-only (migration)."""

    __tablename__ = "outbox"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    exchange: Mapped[str]
    routing_key: Mapped[str]
    aggregate_type: Mapped[str]
    aggregate_id: Mapped[str]
    aggregate_version: Mapped[int]
    payload: Mapped[dict] = mapped_column(JSONB)
    occurred_at: Mapped[datetime]
    published_at: Mapped[datetime | None]
    attempts: Mapped[int] = mapped_column(default=0)
    last_error: Mapped[str | None]
