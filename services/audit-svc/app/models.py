"""SQLAlchemy models for the ``audit`` schema (LLD-04 §2).

The append-only trigger, the REVOKE grants, and the seq sequence are
migration-only — not mapped here.
"""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, FetchedValue, LargeBinary, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    type_annotation_map = {datetime: DateTime(timezone=True)}


class AuditEvent(Base):
    """One append-only row per producer-emitted audit event.

    ``id`` == producer-generated ``audit_id`` (the dedupe key from the
    envelope payload). ``seq`` is a monotonic bigserial used for keyset
    pagination and hash-chain ordering.
    """

    __tablename__ = "audit_events"
    __table_args__ = (
        CheckConstraint("result IN ('success','denied','failed')", name="ck_audit_events_result"),
        CheckConstraint("severity IN ('info','notice','high')", name="ck_audit_events_severity"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    # server_default=FetchedValue(): the column's actual default lives on the
    # DB sequence (migration-only), not here — this just tells the ORM to omit
    # `seq` from the INSERT column list rather than send an explicit NULL.
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=FetchedValue())
    occurred_at: Mapped[datetime]
    recorded_at: Mapped[datetime]
    actor: Mapped[dict] = mapped_column(JSONB, nullable=False)
    on_behalf_of: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    domain: Mapped[str]
    action: Mapped[str]
    target: Mapped[dict] = mapped_column(JSONB, nullable=False)
    result: Mapped[str]
    reason: Mapped[str | None]
    changes: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    source: Mapped[dict] = mapped_column(JSONB, nullable=False)
    correlation_id: Mapped[str | None]
    trace_id: Mapped[str | None]
    client: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    severity: Mapped[str] = mapped_column(Text, default="info")
    prev_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)


class Inbox(Base):
    """Standard InboxConsumer dedup table (LLD-08 §6)."""

    __tablename__ = "inbox"

    consumer: Mapped[str] = mapped_column(Text, primary_key=True)
    message_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    received_at: Mapped[datetime]
