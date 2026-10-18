"""seed role/permission catalog, compiled into casbin_rule p-rows (LLD-02 §3.1, §4)

Revision ID: 5830e6a07042
Revises: a59d76de1f75
Create Date: 2026-10-18 00:00:01.000000

"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision: str = "5830e6a07042"
down_revision: str | Sequence[str] | None = "a59d76de1f75"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

roles_table = sa.table(
    "roles",
    sa.column("id", sa.Text()),
    sa.column("scope", sa.Text()),
    sa.column("description", sa.Text()),
    sa.column("requires_mfa", sa.Boolean()),
    sa.column("assignable_by", sa.ARRAY(sa.Text())),
    sa.column("system", sa.Boolean()),
)

permissions_table = sa.table(
    "permissions",
    sa.column("id", sa.Text()),
    sa.column("resource", sa.Text()),
    sa.column("action", sa.Text()),
)

role_permissions_table = sa.table(
    "role_permissions",
    sa.column("role_id", sa.Text()),
    sa.column("permission_id", sa.Text()),
    sa.column("effect", sa.Text()),
)

casbin_rule_table = sa.table(
    "casbin_rule",
    sa.column("ptype", sa.String()),
    sa.column("v0", sa.String()),
    sa.column("v1", sa.String()),
    sa.column("v2", sa.String()),
    sa.column("v3", sa.String()),
    sa.column("v4", sa.String()),
)

policy_version_table = sa.table(
    "policy_version",
    sa.column("id", sa.Integer()),
    sa.column("version", sa.Integer()),
    sa.column("epoch", PG_UUID(as_uuid=False)),
    sa.column("updated_at", sa.DateTime(timezone=True)),
)

# --- the catalog (LLD-02 §4) -------------------------------------------------

ROLES: list[dict[str, object]] = [
    {
        "id": "org_admin",
        "scope": "org",
        "description": "Full administration of one organization: settings, members, invitations.",
        "requires_mfa": False,
        "assignable_by": ["org_admin"],
        "system": False,
    },
    {
        "id": "catalog_manager",
        "scope": "org",
        "description": "Manages products, categories, and promotions.",
        "requires_mfa": False,
        "assignable_by": ["org_admin"],
        "system": False,
    },
    {
        "id": "inventory_manager",
        "scope": "org",
        "description": "Manages stock levels and reservations.",
        "requires_mfa": False,
        "assignable_by": ["org_admin"],
        "system": False,
    },
    {
        "id": "order_manager",
        "scope": "org",
        "description": "Manages order lifecycle and refunds.",
        "requires_mfa": False,
        "assignable_by": ["org_admin"],
        "system": False,
    },
    {
        "id": "customer",
        "scope": "customer",
        "description": "Platform-level customer: own cart and orders. Granted on signup.",
        "requires_mfa": False,
        "assignable_by": [],
        "system": True,
    },
    {
        "id": "platform_operator",
        "scope": "platform",
        "description": "Platform operations: DLQ replay, reconciliation, feature flags.",
        "requires_mfa": True,
        "assignable_by": ["super_user"],
        "system": False,
    },
    {
        "id": "super_user",
        "scope": "platform",
        "description": "Full platform administration, including identity_config and any grant.",
        "requires_mfa": True,
        "assignable_by": ["super_user"],
        "system": True,
    },
]

# (role_id, resource, action, effect)
ROLE_PERMISSIONS: list[tuple[str, str, str, str]] = [
    ("org_admin", "org_settings", "read", "allow"),
    ("org_admin", "org_settings", "update", "allow"),
    ("org_admin", "member", "*", "allow"),
    ("org_admin", "invitation", "*", "allow"),
    ("org_admin", "role_assignment", "read", "allow"),
    ("org_admin", "role_assignment", "assign", "allow"),
    ("org_admin", "role_assignment", "revoke", "allow"),
    ("org_admin", "product", "read", "allow"),
    ("org_admin", "stock", "read", "allow"),
    ("org_admin", "order", "read", "allow"),
    ("org_admin", "audit_log", "read", "allow"),
    ("catalog_manager", "product", "*", "allow"),
    ("catalog_manager", "category", "*", "allow"),
    ("catalog_manager", "promotion", "*", "allow"),
    ("inventory_manager", "product", "read", "allow"),
    ("inventory_manager", "stock", "read", "allow"),
    ("inventory_manager", "stock", "adjust", "allow"),
    ("inventory_manager", "reservation", "read", "allow"),
    ("inventory_manager", "reservation", "release", "allow"),
    ("order_manager", "product", "read", "allow"),
    ("order_manager", "stock", "read", "allow"),
    ("order_manager", "order", "read", "allow"),
    ("order_manager", "order", "update_status", "allow"),
    ("order_manager", "order", "cancel", "allow"),
    ("order_manager", "refund", "create", "allow"),
    ("order_manager", "reservation", "read", "allow"),
    ("customer", "cart", "*", "allow"),
    ("customer", "order", "create", "allow"),
    ("customer", "order", "read", "allow"),
    ("customer", "order", "cancel", "allow"),
    ("platform_operator", "platform_user", "read", "allow"),
    ("platform_operator", "platform_org", "read", "allow"),
    ("platform_operator", "dlq", "*", "allow"),
    ("platform_operator", "reconciliation", "*", "allow"),
    ("platform_operator", "feature_flag", "*", "allow"),
    ("platform_operator", "identity_config", "*", "deny"),
    ("super_user", "*", "*", "allow"),
]

#: role_id -> domain used for its p/g rows ("*" means "any org" for org-scope roles).
ROLE_DOMAIN: dict[str, str] = {
    "org_admin": "*",
    "catalog_manager": "*",
    "inventory_manager": "*",
    "order_manager": "*",
    "customer": "customer",
    "platform_operator": "platform",
    "super_user": "platform",
}


def upgrade() -> None:
    op.bulk_insert(roles_table, ROLES)

    permission_ids: set[str] = set()
    role_permission_rows: list[dict[str, object]] = []
    casbin_p_rows: list[dict[str, object]] = []

    for role_id, resource, action, effect in ROLE_PERMISSIONS:
        permission_id = f"{resource}:{action}"
        permission_ids.add(permission_id)
        role_permission_rows.append(
            {"role_id": role_id, "permission_id": permission_id, "effect": effect}
        )
        casbin_p_rows.append(
            {
                "ptype": "p",
                "v0": f"role:{role_id}",
                "v1": ROLE_DOMAIN[role_id],
                "v2": resource,
                "v3": action,
                "v4": effect,
            }
        )

    op.bulk_insert(
        permissions_table,
        [
            {"id": pid, "resource": pid.rsplit(":", 1)[0], "action": pid.rsplit(":", 1)[1]}
            for pid in sorted(permission_ids)
        ],
    )
    op.bulk_insert(role_permissions_table, role_permission_rows)
    op.bulk_insert(casbin_rule_table, casbin_p_rows)

    # role_grant:<role> permissions, compiled from each role's assignable_by
    # (LLD-02 §11 rule 2) — any service can check grant rights locally.
    grant_rows: list[dict[str, object]] = []
    for role in ROLES:
        role_id = str(role["id"])
        for grantor_role_id in role["assignable_by"]:  # type: ignore[union-attr]
            grant_rows.append(
                {
                    "ptype": "p",
                    "v0": f"role:{grantor_role_id}",
                    "v1": ROLE_DOMAIN[str(grantor_role_id)],
                    "v2": f"role_grant:{role_id}",
                    "v3": "assign",
                    "v4": "allow",
                }
            )
    op.bulk_insert(casbin_rule_table, grant_rows)

    op.bulk_insert(
        policy_version_table,
        [{"id": 1, "version": 1, "epoch": str(uuid.uuid4()), "updated_at": datetime.now(UTC)}],
    )


def downgrade() -> None:
    op.execute("DELETE FROM policy_version WHERE id = 1")
    op.execute("DELETE FROM casbin_rule WHERE ptype = 'p'")
    op.execute("DELETE FROM role_permissions")
    op.execute("DELETE FROM permissions")
    op.execute("DELETE FROM roles")
