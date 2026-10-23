"""add inbox table for idempotent event consumption (LLD-08 §6)

Revision ID: 7c1a5e9d2f34
Revises: 823aca443ba7
Create Date: 2026-10-23 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "7c1a5e9d2f34"
down_revision: str | Sequence[str] | None = "823aca443ba7"
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
    # 14-day retention (LLD-08 §6) is a deferred open item, same as the
    # other services' identical tables -- no sweeper job yet.


def downgrade() -> None:
    op.drop_table("inbox")
