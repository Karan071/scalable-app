"""add inbox table for idempotent event consumption (LLD-08 §6)

Revision ID: 4b2f9b6e1a08
Revises: eaaa43ecea5c
Create Date: 2026-10-23 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "4b2f9b6e1a08"
down_revision: str | Sequence[str] | None = "eaaa43ecea5c"
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
    # 14-day retention (LLD-08 §6) is a deferred open item, same as
    # authz-svc's identical table -- no sweeper job yet.


def downgrade() -> None:
    op.drop_table("inbox")
