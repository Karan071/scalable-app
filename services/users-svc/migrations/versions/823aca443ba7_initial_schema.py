"""initial schema: users, reserved_handles, outbox (LLD-01 §11.1, LLD-08 §5.1)

Revision ID: 823aca443ba7
Revises:
Create Date: 2026-10-18 00:50:49.088184

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "823aca443ba7"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RESERVED_HANDLES = ["admin", "support", "scaleforge", "staff", "root", "api"]


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("idp_issuer", sa.Text(), nullable=False),
        sa.Column("idp_subject", sa.Text(), nullable=False),
        sa.Column("email", postgresql.CITEXT(), nullable=False),
        sa.Column("email_verified", sa.Boolean(), nullable=False),
        sa.Column("handle", sa.Text(), nullable=True),
        sa.Column("handle_normalized", sa.Text(), nullable=True),
        sa.Column("display_name", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("status_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.UniqueConstraint("idp_issuer", "idp_subject", name="uq_users_idp_identity"),
        sa.UniqueConstraint("handle_normalized", name="uq_users_handle_normalized"),
        sa.CheckConstraint(
            "status IN ('active','disabled','deletion_pending','anonymized')",
            name="ck_users_status",
        ),
    )

    op.create_table(
        "reserved_handles",
        sa.Column("handle_normalized", sa.Text(), primary_key=True),
    )
    op.bulk_insert(
        sa.table("reserved_handles", sa.column("handle_normalized", sa.Text())),
        [{"handle_normalized": h} for h in _RESERVED_HANDLES],
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
    # Partial index for the relay's claim query (LLD-08 §5.1/§5.3): CONCURRENTLY
    # needs autocommit, unavailable inside Alembic's migration transaction, so
    # this table starts empty and the table-create lock already covers it.
    op.create_index(
        "ix_outbox_unpublished",
        "outbox",
        ["occurred_at", "id"],
        postgresql_where=sa.text("published_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_table("outbox")
    op.drop_table("reserved_handles")
    op.drop_table("users")
