"""initial schema: categories, products, skus, promotions, price_history,
store_pricing_settings, outbox (LLD-05 §2, LLD-08 §5.1)

Revision ID: c1a3b7d9e021
Revises:
Create Date: 2026-10-24 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c1a3b7d9e021"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "categories",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("parent_id", postgresql.UUID(as_uuid=True), nullable=True),
        # ltree has no first-class SQLAlchemy type (app/models.py's LTree);
        # create as TEXT and convert so the column keeps its default/NOT NULL.
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("org_id", "parent_id", "slug", name="uq_categories_org_parent_slug"),
    )
    op.execute("ALTER TABLE categories ALTER COLUMN path TYPE ltree USING path::ltree")
    op.execute(
        "ALTER TABLE categories ADD CONSTRAINT ck_categories_max_depth CHECK (nlevel(path) <= 4)"
    )
    op.create_index("ix_categories_path", "categories", ["path"], postgresql_using="gist")
    op.create_index("ix_categories_org_id", "categories", ["org_id"])

    op.create_table(
        "products",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description_md", sa.Text(), nullable=False, server_default=""),
        sa.Column("description_html", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("category_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("org_id", "slug", name="uq_products_org_slug"),
        sa.CheckConstraint("status IN ('draft','active','archived')", name="ck_products_status"),
    )
    op.create_index(
        "ix_products_org_status_updated",
        "products",
        ["org_id", "status", sa.text("updated_at DESC"), "id"],
    )

    op.create_table(
        "skus",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("product_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sku_code", sa.Text(), nullable=False),
        sa.Column("attributes", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("price_minor", sa.BigInteger(), nullable=False),
        sa.Column("compare_at_minor", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("org_id", "sku_code", name="uq_skus_org_code"),
        sa.CheckConstraint("price_minor >= 0", name="ck_skus_price_minor"),
        sa.CheckConstraint(
            "compare_at_minor IS NULL OR compare_at_minor >= price_minor",
            name="ck_skus_compare_at",
        ),
        sa.CheckConstraint("status IN ('active','archived')", name="ck_skus_status"),
    )
    op.create_index("ix_skus_product_id", "skus", ["product_id"])

    op.create_table(
        "promotions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("value", sa.Integer(), nullable=False),
        sa.Column("min_subtotal_minor", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("org_id", "code", name="uq_promotions_org_code"),
        sa.CheckConstraint("kind IN ('percent','fixed')", name="ck_promotions_kind"),
        sa.CheckConstraint(
            "(kind = 'percent' AND value BETWEEN 1 AND 90) OR (kind = 'fixed' AND value > 0)",
            name="ck_promotions_value",
        ),
        sa.CheckConstraint("status IN ('active','disabled')", name="ck_promotions_status"),
    )

    op.create_table(
        "price_history",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sku_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("old_price_minor", sa.BigInteger(), nullable=False),
        sa.Column("new_price_minor", sa.BigInteger(), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_price_history_sku_id", "price_history", ["sku_id"])

    op.create_table(
        "store_pricing_settings",
        sa.Column("org_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("shipping_flat_minor", sa.BigInteger(), nullable=False),
        sa.Column("tax_rate_bps", sa.Integer(), nullable=False),
        sa.Column("source_version", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
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


def downgrade() -> None:
    op.drop_table("outbox")
    op.drop_table("store_pricing_settings")
    op.drop_table("price_history")
    op.drop_table("promotions")
    op.drop_table("skus")
    op.drop_table("products")
    op.drop_table("categories")
