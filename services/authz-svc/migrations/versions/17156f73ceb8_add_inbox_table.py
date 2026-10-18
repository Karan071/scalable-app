"""add inbox table for idempotent event consumption (LLD-08 §6)

Revision ID: 17156f73ceb8
Revises: 5830e6a07042
Create Date: 2026-10-18 00:00:02.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "17156f73ceb8"
down_revision: str | Sequence[str] | None = "5830e6a07042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
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
    # No authz_reader grant needed: write-path-only, never read by the
    # enforcer (same as outbox itself needed no reader grant). 14-day
    # retention (LLD-08 §6) is a deferred open item — no sweeper job yet.


def downgrade() -> None:
    op.drop_table("inbox")
