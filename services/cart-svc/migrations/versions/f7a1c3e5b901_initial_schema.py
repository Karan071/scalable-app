"""initial schema: carts, cart_items, inbox (LLD-06 §2, LLD-08 §6)

Revision ID: f7a1c3e5b901
Revises:
Create Date: 2026-10-24 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f7a1c3e5b901"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "carts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("customer_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('active','checked_out','abandoned')", name="ck_carts_status"
        ),
    )
    # Partial unique: only one *active* cart per (customer, org) -- a
    # checked-out or abandoned cart must not block a new active one.
    op.create_index(
        "uq_carts_customer_org_active",
        "carts",
        ["customer_id", "org_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index("ix_carts_status_expires", "carts", ["status", "expires_at"])

    op.create_table(
        "cart_items",
        sa.Column("cart_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("sku_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("product_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("qty", sa.Integer(), nullable=False),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("qty BETWEEN 1 AND 99", name="ck_cart_items_qty"),
        sa.ForeignKeyConstraint(["cart_id"], ["carts.id"], ondelete="CASCADE"),
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
    # Unused until LLD-06 §2's order.confirmed.v1 consumer is built
    # (deferred: orders-svc, its publisher, doesn't exist yet). 14-day
    # retention is the same deferred open item every other service's
    # identical table has -- no sweeper job yet.


def downgrade() -> None:
    op.drop_table("inbox")
    op.drop_table("cart_items")
    op.drop_table("carts")
