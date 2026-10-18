"""initial authz schema: roles, role_assignments, casbin_rule, policy_version, outbox (LLD-02 §7)

Revision ID: a59d76de1f75
Revises:
Create Date: 2026-10-18 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a59d76de1f75"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "roles",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("requires_mfa", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column(
            "assignable_by", postgresql.ARRAY(sa.Text()), nullable=False, server_default="{}"
        ),
        sa.Column("system", sa.Boolean(), nullable=False, server_default="false"),
        sa.CheckConstraint("scope IN ('platform','org','customer')", name="ck_roles_scope"),
    )

    op.create_table(
        "permissions",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("resource", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
    )

    op.create_table(
        "role_permissions",
        sa.Column("role_id", sa.Text(), primary_key=True),
        sa.Column("permission_id", sa.Text(), primary_key=True),
        sa.Column("effect", sa.Text(), nullable=False),
        sa.CheckConstraint("effect IN ('allow','deny')", name="ck_role_permissions_effect"),
    )

    op.create_table(
        "role_assignments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role_id", sa.Text(), nullable=False),
        sa.Column("domain", sa.Text(), nullable=False),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("membership_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("granted_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "status IN ('active','suspended','revoked')", name="ck_role_assignments_status"
        ),
    )
    # Partial uniqueness: a (user, role, domain) triple may have at most one
    # non-revoked assignment (LLD-02 §7) — revoking and re-granting is fine.
    op.create_index(
        "uq_role_assignments_active",
        "role_assignments",
        ["user_id", "role_id", "domain"],
        unique=True,
        postgresql_where=sa.text("status <> 'revoked'"),
    )
    op.create_index("ix_role_assignments_domain", "role_assignments", ["domain"])

    # Domain/scope consistency (LLD-02 §7): an org-scope role's domain must be
    # "org:<org_id>"; a non-org-scope role's domain must not reference org_id.
    op.execute(
        """
        CREATE FUNCTION check_role_assignment_scope() RETURNS trigger AS $$
        DECLARE
            role_scope text;
        BEGIN
            SELECT scope INTO role_scope FROM authz.roles WHERE id = NEW.role_id;
            IF role_scope IS NULL THEN
                RAISE EXCEPTION 'unknown role_id %', NEW.role_id;
            END IF;
            IF role_scope = 'org' THEN
                IF NEW.org_id IS NULL OR NEW.domain <> ('org:' || NEW.org_id) THEN
                    RAISE EXCEPTION 'org-scope role % requires domain = org:<org_id>', NEW.role_id;
                END IF;
            ELSE
                IF NEW.org_id IS NOT NULL OR NEW.domain <> role_scope THEN
                    RAISE EXCEPTION '%-scope role % requires domain = %',
                        role_scope, NEW.role_id, role_scope;
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql SET search_path = authz, pg_catalog;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_role_assignments_scope
        BEFORE INSERT OR UPDATE ON role_assignments
        FOR EACH ROW EXECUTE FUNCTION check_role_assignment_scope();
        """
    )

    op.create_table(
        "membership_projection",
        sa.Column("membership_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("source_version", sa.Integer(), nullable=False),
    )

    op.create_table(
        "domain_status",
        sa.Column("domain", sa.Text(), primary_key=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_version", sa.Integer(), nullable=False),
    )

    # Column shape matches casbin_sqlalchemy_adapter's own model exactly (ADR-0004).
    op.create_table(
        "casbin_rule",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ptype", sa.String(length=255), nullable=True),
        sa.Column("v0", sa.String(length=255), nullable=True),
        sa.Column("v1", sa.String(length=255), nullable=True),
        sa.Column("v2", sa.String(length=255), nullable=True),
        sa.Column("v3", sa.String(length=255), nullable=True),
        sa.Column("v4", sa.String(length=255), nullable=True),
        sa.Column("v5", sa.String(length=255), nullable=True),
    )
    op.create_index("ix_casbin_rule_ptype", "casbin_rule", ["ptype"])

    op.create_table(
        "policy_version",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("epoch", postgresql.UUID(as_uuid=True), nullable=False),
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

    # init-schemas.sh grants USAGE on the authz schema to authz_reader and
    # explicitly defers these table-level SELECT grants to this migration.
    op.execute("GRANT SELECT ON casbin_rule, domain_status, policy_version TO authz_reader")


def downgrade() -> None:
    op.execute("REVOKE SELECT ON casbin_rule, domain_status, policy_version FROM authz_reader")
    op.drop_table("outbox")
    op.drop_table("policy_version")
    op.drop_table("casbin_rule")
    op.drop_table("domain_status")
    op.drop_table("membership_projection")
    op.execute("DROP TRIGGER IF EXISTS trg_role_assignments_scope ON role_assignments")
    op.execute("DROP FUNCTION IF EXISTS check_role_assignment_scope")
    op.drop_table("role_assignments")
    op.drop_table("role_permissions")
    op.drop_table("permissions")
    op.drop_table("roles")
