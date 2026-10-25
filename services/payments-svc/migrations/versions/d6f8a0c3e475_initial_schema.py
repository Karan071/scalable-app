"""initial schema: payment_attempts, payment_events, provider_webhooks,
refunds, outbox, inbox (LLD-06 §7.1, LLD-08 §5.1, §6)

Revision ID: d6f8a0c3e475
Revises:
Create Date: 2026-10-25 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.models import PAYMENT_STATUSES
from sqlalchemy.dialects import postgresql

revision: str = "d6f8a0c3e475"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "payment_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("customer_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("amount_minor", sa.Integer(), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("provider_intent_id", sa.Text(), nullable=True),
        sa.Column("provider_sequence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pm_token_ref", sa.Text(), nullable=False),
        sa.Column("authorized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(f"status IN {PAYMENT_STATUSES!r}", name="ck_payment_attempts_status"),
        sa.UniqueConstraint("provider_intent_id", name="uq_payment_attempts_provider_intent"),
    )
    # LLD-06 §7.1: "Partial UNIQUE(order_id) WHERE status NOT IN
    # ('declined','voided')" -- a fresh attempt is allowed after a
    # declined/voided one, but never two live attempts for the same order.
    op.create_index(
        "uq_payment_attempts_order_active",
        "payment_attempts",
        ["order_id"],
        unique=True,
        postgresql_where=sa.text("status NOT IN ('declined', 'voided')"),
    )
    op.create_index(
        "ix_payment_attempts_status_updated", "payment_attempts", ["status", "updated_at"]
    )

    op.create_table(
        "payment_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("payment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("amount_minor", sa.Integer(), nullable=False),
        sa.Column("provider_event_id", sa.Text(), nullable=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["payment_id"], ["payment_attempts.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_payment_events_payment", "payment_events", ["payment_id", "at"])

    op.create_table(
        "provider_webhooks",
        sa.Column("provider_event_id", sa.Text(), primary_key=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("signature_valid", sa.Boolean(), nullable=False),
        sa.Column("payload_sha256", sa.Text(), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result", sa.Text(), nullable=True),
    )

    op.create_table(
        "refunds",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("payment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("amount_minor", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("idempotency_key", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider_refund_id", sa.Text(), nullable=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("idempotency_key", name="uq_refunds_idempotency_key"),
        sa.ForeignKeyConstraint(["payment_id"], ["payment_attempts.id"], ondelete="CASCADE"),
    )

    op.create_table(
        "outbox",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("exchange", sa.Text(), nullable=False),
        sa.Column("routing_key", sa.Text(), nullable=False),
        sa.Column("aggregate_type", sa.Text(), nullable=False),
        sa.Column("aggregate_id", sa.Text(), nullable=False),
        sa.Column("aggregate_version", sa.Integer(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_outbox_unpublished",
        "outbox",
        ["occurred_at", "id"],
        postgresql_where=sa.text("published_at IS NULL"),
    )

    op.create_table(
        "inbox",
        sa.Column("consumer", sa.Text(), primary_key=True),
        sa.Column("message_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    # 14-day retention documented, not implemented -- the same deferral
    # every other service's inbox table carries.


def downgrade() -> None:
    op.drop_table("inbox")
    op.drop_table("outbox")
    op.drop_table("refunds")
    op.drop_table("provider_webhooks")
    op.drop_table("payment_events")
    op.drop_table("payment_attempts")
