"""SQLAlchemy models for the ``authz`` schema (LLD-02 §7) and its outbox (LLD-08 §5.1).

Partial unique indexes, the domain/scope-consistency trigger, and the
``authz_reader`` grants are DDL-only (migration), not mapped here — same
precedent as orgs-svc's ``invitations`` partial index.
"""

import uuid
from datetime import datetime

from sqlalchemy import ARRAY, CheckConstraint, DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    type_annotation_map = {datetime: DateTime(timezone=True)}


class Role(Base):
    __tablename__ = "roles"
    __table_args__ = (
        CheckConstraint("scope IN ('platform','org','customer')", name="ck_roles_scope"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    scope: Mapped[str]
    description: Mapped[str]
    requires_mfa: Mapped[bool] = mapped_column(default=False)
    assignable_by: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    system: Mapped[bool] = mapped_column(default=False)


class Permission(Base):
    __tablename__ = "permissions"

    id: Mapped[str] = mapped_column(Text, primary_key=True)  # "<resource>:<action>"
    resource: Mapped[str]
    action: Mapped[str]


class RolePermission(Base):
    __tablename__ = "role_permissions"
    __table_args__ = (
        CheckConstraint("effect IN ('allow','deny')", name="ck_role_permissions_effect"),
    )

    role_id: Mapped[str] = mapped_column(Text, primary_key=True)
    permission_id: Mapped[str] = mapped_column(Text, primary_key=True)
    effect: Mapped[str]


class RoleAssignment(Base):
    """``status <> 'revoked'`` partial-uniqueness on ``(user_id, role_id, domain)``
    and the domain/scope-consistency trigger are migration-only (LLD-02 §7)."""

    __tablename__ = "role_assignments"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active','suspended','revoked')", name="ck_role_assignments_status"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    user_id: Mapped[uuid.UUID]
    role_id: Mapped[str]
    domain: Mapped[str]
    org_id: Mapped[uuid.UUID | None]
    membership_id: Mapped[uuid.UUID | None]
    status: Mapped[str]
    granted_by: Mapped[uuid.UUID]
    granted_at: Mapped[datetime]
    revoked_by: Mapped[uuid.UUID | None]
    revoked_at: Mapped[datetime | None]
    reason: Mapped[str | None]
    version: Mapped[int]


class MembershipProjection(Base):
    """Fed by ``membership.*`` events (LLD-02 §9). Until E2.8 ships real event
    consumption, this is upserted synchronously from an orgs-svc call at
    role-assignment write time (an interim, forward-compatible substitute —
    see ``service.py``)."""

    __tablename__ = "membership_projection"

    membership_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    org_id: Mapped[uuid.UUID]
    user_id: Mapped[uuid.UUID]
    status: Mapped[str]
    source_version: Mapped[int]


class DomainStatus(Base):
    """Fed by ``organization.*`` events (LLD-02 §9) — same E2.8 dependency as above.
    Read directly by ``sf_authz.PolicyEnforcer`` via the ``authz_reader`` role."""

    __tablename__ = "domain_status"

    domain: Mapped[str] = mapped_column(Text, primary_key=True)
    status: Mapped[str]
    updated_at: Mapped[datetime]
    source_version: Mapped[int]


class CasbinRule(Base):
    """Column shape matches ``casbin_sqlalchemy_adapter``'s own model exactly
    (ADR-0004): the adapter reads/writes this table directly by name."""

    __tablename__ = "casbin_rule"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ptype: Mapped[str] = mapped_column(String(255))
    v0: Mapped[str | None] = mapped_column(String(255))
    v1: Mapped[str | None] = mapped_column(String(255))
    v2: Mapped[str | None] = mapped_column(String(255))
    v3: Mapped[str | None] = mapped_column(String(255))
    v4: Mapped[str | None] = mapped_column(String(255))
    v5: Mapped[str | None] = mapped_column(String(255))


class PolicyVersion(Base):
    """Single row, ``id = 1`` (ADR-0004 §3)."""

    __tablename__ = "policy_version"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version: Mapped[int] = mapped_column(default=0)
    epoch: Mapped[uuid.UUID]
    updated_at: Mapped[datetime]


class Outbox(Base):
    """LLD-08 §5.1. The partial index on unpublished rows is DDL-only (migration)."""

    __tablename__ = "outbox"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    exchange: Mapped[str]
    routing_key: Mapped[str]
    aggregate_type: Mapped[str]
    aggregate_id: Mapped[str]
    aggregate_version: Mapped[int]
    payload: Mapped[dict] = mapped_column(JSONB)
    occurred_at: Mapped[datetime]
    published_at: Mapped[datetime | None]
    attempts: Mapped[int] = mapped_column(default=0)
    last_error: Mapped[str | None]
