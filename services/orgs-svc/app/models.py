"""SQLAlchemy models for the ``orgs`` schema (LLD-03 §3) and its outbox (LLD-08 §5.1)."""

import uuid
from datetime import datetime

from sqlalchemy import ARRAY, CheckConstraint, DateTime, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import CITEXT, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    # Every migration column is timestamptz (LLD-13 §6: UTC everywhere); see
    # users-svc/app/models.py for why this mapping is needed.
    type_annotation_map = {datetime: DateTime(timezone=True)}


class Organization(Base):
    __tablename__ = "organizations"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_organizations_slug"),
        CheckConstraint(
            "status IN ('pending','active','suspended','archived')",
            name="ck_organizations_status",
        ),
        CheckConstraint("tax_rate_bps BETWEEN 0 AND 10000", name="ck_organizations_tax_rate"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    slug: Mapped[str]
    name: Mapped[str]
    status: Mapped[str]
    status_reason: Mapped[str | None]
    currency: Mapped[str]
    shipping_flat_minor: Mapped[int]
    tax_rate_bps: Mapped[int]
    settings: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_by: Mapped[uuid.UUID]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
    version: Mapped[int]


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (
        UniqueConstraint("org_id", "user_id", name="uq_memberships_org_user"),
        CheckConstraint("status IN ('active','disabled','revoked')", name="ck_memberships_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    org_id: Mapped[uuid.UUID]
    user_id: Mapped[uuid.UUID]
    status: Mapped[str]
    status_reason: Mapped[str | None]
    invited_by: Mapped[uuid.UUID | None]
    joined_at: Mapped[datetime | None]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
    version: Mapped[int]


class Invitation(Base):
    __tablename__ = "invitations"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_invitations_token_hash"),
        CheckConstraint(
            "status IN ('pending','accepted','revoked','expired')", name="ck_invitations_status"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    org_id: Mapped[uuid.UUID]
    email: Mapped[str] = mapped_column(CITEXT)
    token_hash: Mapped[bytes]
    roles_requested: Mapped[list[str]] = mapped_column(ARRAY(Text))
    status: Mapped[str]
    expires_at: Mapped[datetime]
    invited_by: Mapped[uuid.UUID]
    accepted_by: Mapped[uuid.UUID | None]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
    version: Mapped[int]


class Outbox(Base):
    """LLD-08 §5.1.

    The partial index on unpublished rows is DDL-only (migration), not mapped here.
    """

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
