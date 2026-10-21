"""initial audit schema: audit_events, inbox (LLD-04 §2)

Revision ID: 3f8a2b1c9d4e
Revises:
Create Date: 2026-10-21 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "3f8a2b1c9d4e"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_events",
        # audit_id is the producer-generated dedupe key (LLD-04 §2.1).
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("seq", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("actor", postgresql.JSONB(), nullable=False),
        sa.Column("on_behalf_of", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("domain", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("target", postgresql.JSONB(), nullable=False),
        sa.Column("result", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("changes", postgresql.JSONB(), nullable=True),
        sa.Column("source", postgresql.JSONB(), nullable=False),
        sa.Column("correlation_id", sa.Text(), nullable=True),
        sa.Column("trace_id", sa.Text(), nullable=True),
        sa.Column("client", postgresql.JSONB(), nullable=True),
        sa.Column("severity", sa.Text(), nullable=False, server_default="info"),
        # Hash chain (LLD-04 §2.2): SHA-256(prev_hash || canonical_json(event)).
        # The chain is single-writer: audit-svc acquires a pg_advisory_xact_lock
        # before every insert, which serialises writes without a dedicated sequence
        # gap (a crash resets the advisory lock, so no hole is left).
        sa.Column("prev_hash", sa.LargeBinary(), nullable=False),
        sa.Column("hash", sa.LargeBinary(), nullable=False),
        sa.CheckConstraint(
            "result IN ('success','denied','failed')", name="ck_audit_events_result"
        ),
        sa.CheckConstraint("severity IN ('info','notice','high')", name="ck_audit_events_severity"),
    )
    # seq is not a PK (id is), but must be monotonically orderable — backed by a sequence.
    # asyncpg's prepared-statement protocol rejects multiple commands in one
    # execute() call, so these must be separate op.execute() invocations.
    op.execute("CREATE SEQUENCE audit_events_seq_seq START 1")
    op.execute(
        "ALTER TABLE audit_events ALTER COLUMN seq SET DEFAULT nextval('audit_events_seq_seq')"
    )
    op.create_index("ix_audit_events_seq", "audit_events", ["seq"], unique=True)
    op.create_index(
        "ix_audit_events_domain_occurred_at",
        "audit_events",
        ["domain", sa.text("occurred_at DESC")],
    )
    op.create_index(
        "ix_audit_events_actor_id",
        "audit_events",
        [sa.text("(actor->>'id')"), sa.text("occurred_at DESC")],
    )
    op.create_index(
        "ix_audit_events_target",
        "audit_events",
        [sa.text("(target->>'type')"), sa.text("(target->>'id')"), sa.text("occurred_at DESC")],
    )

    # Append-only enforcement (LLD-04 §2.2): svc_audit may only INSERT and SELECT.
    # UPDATE, DELETE, and TRUNCATE are revoked; the trigger is a belt-and-suspenders
    # guard against a future privilege escalation.
    op.execute("REVOKE UPDATE, DELETE, TRUNCATE ON audit_events FROM svc_audit")
    op.execute("""
        CREATE OR REPLACE FUNCTION reject_audit_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'audit_events is append-only';
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER trg_audit_events_append_only
        BEFORE UPDATE OR DELETE ON audit_events
        FOR EACH ROW EXECUTE FUNCTION reject_audit_mutation()
    """)

    # Standard inbox for InboxConsumer dedup (LLD-08 §6).
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


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_audit_events_append_only ON audit_events")
    op.execute("DROP FUNCTION IF EXISTS reject_audit_mutation")
    op.drop_table("inbox")
    op.drop_table("audit_events")
    op.execute("DROP SEQUENCE IF EXISTS audit_events_seq_seq")
