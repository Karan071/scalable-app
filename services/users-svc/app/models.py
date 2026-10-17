"""SQLAlchemy models for the ``users`` schema (LLD-01 §11.1) and its outbox (LLD-08 §5.1)."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, UniqueConstraint
from sqlalchemy.dialects.postgresql import CITEXT, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    # Every migration column is `timestamptz` (LLD-13 §6: UTC everywhere);
    # without this, a bare `Mapped[datetime]` compiles binds against a naive
    # `TIMESTAMP` and asyncpg rejects our tz-aware `datetime.now(UTC)` values
    # (confirmed against live Postgres, 2026-10-18).
    type_annotation_map = {datetime: DateTime(timezone=True)}


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("idp_issuer", "idp_subject", name="uq_users_idp_identity"),
        UniqueConstraint("handle_normalized", name="uq_users_handle_normalized"),
        CheckConstraint(
            "status IN ('active','disabled','deletion_pending','anonymized')",
            name="ck_users_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    idp_issuer: Mapped[str]
    idp_subject: Mapped[str]
    email: Mapped[str] = mapped_column(CITEXT)
    email_verified: Mapped[bool]
    handle: Mapped[str | None]
    handle_normalized: Mapped[str | None]
    display_name: Mapped[str | None]
    status: Mapped[str]
    status_reason: Mapped[str | None]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
    disabled_at: Mapped[datetime | None]
    version: Mapped[int]


class ReservedHandle(Base):
    __tablename__ = "reserved_handles"

    handle_normalized: Mapped[str] = mapped_column(primary_key=True)


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
