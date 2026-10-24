"""initial schema: product_search_docs, inbox (LLD-05 §7.1, LLD-08 §6)

Revision ID: e5f7a9c1b245
Revises:
Create Date: 2026-10-24 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e5f7a9c1b245"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "product_search_docs",
        sa.Column("org_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("product_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("primary_image_key", sa.Text(), nullable=True),
        sa.Column(
            "category_ids",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("min_price_minor", sa.BigInteger(), nullable=True),
        sa.Column("max_price_minor", sa.BigInteger(), nullable=True),
        sa.Column("in_stock", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("product_version", sa.Integer(), nullable=False),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    # LLD-05 §7.1: weighted tsvector over name (A) and description (C). No
    # category-name weight (B) yet -- catalog-svc's product.updated.v1
    # doesn't carry category names, only category_id (LLD-05 §4's "As
    # built"). A STORED generated column: Postgres recomputes it itself on
    # every UPDATE, so it can never drift from name/description_text.
    op.execute(
        "ALTER TABLE product_search_docs ADD COLUMN tsv tsvector GENERATED ALWAYS AS ("
        "  setweight(to_tsvector('simple', coalesce(name, '')), 'A') || "
        "  setweight(to_tsvector('simple', coalesce(description_text, '')), 'C')"
        ") STORED"
    )
    op.create_index(
        "ix_product_search_docs_tsv", "product_search_docs", ["tsv"], postgresql_using="gin"
    )
    op.create_index(
        "ix_product_search_docs_name_trgm",
        "product_search_docs",
        ["name"],
        postgresql_using="gin",
        postgresql_ops={"name": "gin_trgm_ops"},
    )
    op.create_index(
        "ix_product_search_docs_category_ids",
        "product_search_docs",
        ["category_ids"],
        postgresql_using="gin",
    )
    op.create_index(
        "ix_product_search_docs_org_created",
        "product_search_docs",
        ["org_id", sa.text("created_at DESC"), "product_id"],
    )
    op.create_index(
        "ix_product_search_docs_org_price",
        "product_search_docs",
        ["org_id", "min_price_minor", "product_id"],
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
    # 14-day retention (LLD-08 §6) is a deferred open item, same as every
    # other service's identical table -- no sweeper job yet. Same for this
    # table's own tombstone rows (LLD-05 §7.2's "kept 7 days").


def downgrade() -> None:
    op.drop_table("inbox")
    op.drop_table("product_search_docs")
