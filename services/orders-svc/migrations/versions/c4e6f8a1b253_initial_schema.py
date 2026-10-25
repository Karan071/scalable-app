"""initial schema: orders, order_lines, order_status_history,
checkout_sagas, idempotency_keys, outbox, inbox (LLD-06 §3.1, §4, §5.2,
LLD-08 §5.1, §6)

Revision ID: c4e6f8a1b253
Revises:
Create Date: 2026-10-25 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from app.models import CANCEL_REASONS, ORDER_STATUSES, PAYMENT_STATUSES, SAGA_STATES
from sqlalchemy.dialects import postgresql

revision: str = "c4e6f8a1b253"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "orders",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("customer_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("payment_status", sa.Text(), nullable=False),
        sa.Column("cart_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("cart_version", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False),
        sa.Column("subtotal_minor", sa.Integer(), nullable=False),
        sa.Column("discount_minor", sa.Integer(), nullable=False),
        sa.Column("shipping_minor", sa.Integer(), nullable=False),
        sa.Column("tax_minor", sa.Integer(), nullable=False),
        sa.Column("total_minor", sa.Integer(), nullable=False),
        sa.Column("promo_code", sa.Text(), nullable=True),
        sa.Column("pricing_fingerprint", sa.Text(), nullable=False),
        sa.Column("shipping_address", postgresql.JSONB(), nullable=False),
        sa.Column("cancel_reason", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("customer_id", "idempotency_key", name="uq_orders_customer_idem_key"),
        sa.CheckConstraint(f"status IN {ORDER_STATUSES!r}", name="ck_orders_status"),
        sa.CheckConstraint(
            f"payment_status IN {PAYMENT_STATUSES!r}", name="ck_orders_payment_status"
        ),
        sa.CheckConstraint(
            f"cancel_reason IS NULL OR cancel_reason IN {CANCEL_REASONS!r}",
            name="ck_orders_cancel_reason",
        ),
        sa.CheckConstraint(
            "total_minor = subtotal_minor - discount_minor + shipping_minor + tax_minor",
            name="ck_orders_total",
        ),
    )
    op.create_index("ix_orders_customer_created", "orders", ["customer_id", "created_at", "id"])
    op.create_index(
        "ix_orders_org_status_created", "orders", ["org_id", "status", "created_at", "id"]
    )
    # LLD-06 §3.1: "Partial UNIQUE(cart_id, cart_version) WHERE status <>
    # 'cancelled'" -- the double-click-two-keys backstop (§4's layer 3).
    op.create_index(
        "uq_orders_cart_version_active",
        "orders",
        ["cart_id", "cart_version"],
        unique=True,
        postgresql_where=sa.text("status <> 'cancelled'"),
    )

    op.create_table(
        "order_lines",
        sa.Column("order_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("line_no", sa.Integer(), primary_key=True),
        sa.Column("sku_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("product_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name_snapshot", sa.Text(), nullable=False),
        sa.Column("unit_price_minor", sa.Integer(), nullable=False),
        sa.Column("qty", sa.Integer(), nullable=False),
        sa.Column("line_total_minor", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "line_total_minor = unit_price_minor * qty", name="ck_order_lines_total"
        ),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="CASCADE"),
    )

    op.create_table(
        "order_status_history",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("from_status", sa.Text(), nullable=True),
        sa.Column("to_status", sa.Text(), nullable=False),
        sa.Column("actor_type", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_order_status_history_order", "order_status_history", ["order_id", "at"])

    op.create_table(
        "checkout_sagas",
        sa.Column("order_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("reservation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("payment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_action_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(f"state IN {SAGA_STATES!r}", name="ck_checkout_sagas_state"),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="CASCADE"),
    )
    # LLD-06 §5.2: the recovery worker's own claim query (service.recover_one).
    op.create_index(
        "ix_checkout_sagas_next_action",
        "checkout_sagas",
        ["next_action_at"],
        postgresql_where=sa.text("state NOT IN ('COMPLETED','CANCELLED')"),
    )

    op.create_table(
        "idempotency_keys",
        sa.Column("scope", sa.Text(), primary_key=True),
        sa.Column("principal_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("key", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resource_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("response_body", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('in_progress','completed')", name="ck_idempotency_keys_status"
        ),
    )
    # 24h retention (LLD-06 §4: "cleanup job") -- documented, not implemented
    # yet, the same deferral every service's inbox retention already carries.
    op.create_index("ix_idempotency_keys_expires", "idempotency_keys", ["expires_at"])

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
    # Shared by both InboxConsumers (q.orders.inventory, q.orders.payments)
    # -- "consumer" is the dedup discriminator, same as every other
    # service's single inbox table. 14-day retention is documented, not
    # implemented (no sweeper job yet), the same deferral every other
    # service's inbox table already carries.


def downgrade() -> None:
    op.drop_table("inbox")
    op.drop_table("outbox")
    op.drop_table("idempotency_keys")
    op.drop_table("checkout_sagas")
    op.drop_table("order_status_history")
    op.drop_table("order_lines")
    op.drop_table("orders")
