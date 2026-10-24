"""initial schema: stock_items, reservations, reservation_lines,
stock_movements, outbox, inbox (LLD-07 §2-§3, LLD-08 §5.1, §6)

Revision ID: b3d5f7a9c142
Revises:
Create Date: 2026-10-24 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b3d5f7a9c142"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "stock_items",
        sa.Column("sku_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("product_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("on_hand", sa.Integer(), nullable=False),
        sa.Column("reserved", sa.Integer(), nullable=False),
        sa.Column("low_stock_threshold", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        # INV-1, INV-2, INV-3 (LLD-07 §2): the database backstop against an
        # over-allocation that a logic bug might otherwise let through.
        sa.CheckConstraint("on_hand >= 0", name="ck_stock_items_on_hand"),
        sa.CheckConstraint("reserved >= 0", name="ck_stock_items_reserved"),
        sa.CheckConstraint("reserved <= on_hand", name="ck_stock_items_reserved_le_on_hand"),
    )
    op.create_index("ix_stock_items_org", "stock_items", ["org_id"])

    op.create_table(
        "reservations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('active','committed','released','expired','rejected')",
            name="ck_reservations_status",
        ),
    )
    # INV-4: one reservation per order. Also the idempotency key for every
    # reserve/commit/release/restock call (LLD-07 §4).
    op.create_unique_constraint("uq_reservations_order_id", "reservations", ["order_id"])
    op.create_index("ix_reservations_org", "reservations", ["org_id"])
    # LLD-07 §6: the sweeper's own claim query is served by this index.
    op.create_index(
        "ix_reservations_active_expires",
        "reservations",
        ["expires_at"],
        postgresql_where=sa.text("status = 'active'"),
    )

    op.create_table(
        "reservation_lines",
        sa.Column("reservation_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("sku_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("qty", sa.Integer(), nullable=False),
        sa.CheckConstraint("qty > 0", name="ck_reservation_lines_qty"),
        sa.ForeignKeyConstraint(["reservation_id"], ["reservations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["sku_id"], ["stock_items.sku_id"]),
    )

    op.create_table(
        "stock_movements",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sku_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("movement_type", sa.Text(), nullable=False),
        sa.Column("delta_on_hand", sa.Integer(), nullable=False),
        sa.Column("delta_reserved", sa.Integer(), nullable=False),
        sa.Column("ref_type", sa.Text(), nullable=False),
        sa.Column("ref_id", sa.Text(), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "movement_type IN ('reserve','release','expire','commit','restock','adjust')",
            name="ck_stock_movements_type",
        ),
    )
    # INV-5: each stock movement is applied once -- the idempotency backstop
    # for every operation in LLD-07 §4 under replay/redelivery.
    op.create_unique_constraint(
        "uq_stock_movements_ref",
        "stock_movements",
        ["ref_type", "ref_id", "movement_type", "sku_id"],
    )
    op.create_index("ix_stock_movements_sku", "stock_movements", ["sku_id"])

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
    # One table serves both InboxConsumers (q.inventory.catalog,
    # q.inventory.commands) -- "consumer" is the dedup discriminator, same
    # pattern as every other service's single inbox table. 14-day retention
    # is documented, not implemented (no sweeper job yet), the same deferral
    # every other service's inbox table already carries.


def downgrade() -> None:
    op.drop_table("inbox")
    op.drop_table("outbox")
    op.drop_table("stock_movements")
    op.drop_table("reservation_lines")
    op.drop_table("reservations")
    op.drop_table("stock_items")
