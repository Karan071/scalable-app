"""SQLAlchemy models for the ``inventory`` schema (LLD-07 §3) and its outbox (LLD-08 §5.1).

INV-1..INV-5 (LLD-07 §2) are database ``CHECK``/``UNIQUE`` constraints, not
just application logic -- they are the final backstop against a logic bug
over-allocating stock. They live in the migration (DDL), not here.
"""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    # Every migration column is timestamptz (LLD-13 §6: UTC everywhere); see
    # users-svc/app/models.py for why this mapping is needed.
    type_annotation_map = {datetime: DateTime(timezone=True)}


class StockItem(Base):
    __tablename__ = "stock_items"
    __table_args__ = (
        CheckConstraint("on_hand >= 0", name="ck_stock_items_on_hand"),
        CheckConstraint("reserved >= 0", name="ck_stock_items_reserved"),
        CheckConstraint("reserved <= on_hand", name="ck_stock_items_reserved_le_on_hand"),
    )

    sku_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    org_id: Mapped[uuid.UUID]
    product_id: Mapped[uuid.UUID]
    on_hand: Mapped[int]
    reserved: Mapped[int]
    low_stock_threshold: Mapped[int]
    version: Mapped[int]
    updated_at: Mapped[datetime]


class Reservation(Base):
    __tablename__ = "reservations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active','committed','released','expired','rejected')",
            name="ck_reservations_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    org_id: Mapped[uuid.UUID]
    order_id: Mapped[uuid.UUID] = mapped_column(unique=True)
    status: Mapped[str]
    #: Irrelevant once a reservation leaves ``active`` (set to ``now()`` for
    #: rejections and tombstones); the sweeper's index only covers ``active``.
    expires_at: Mapped[datetime]
    #: JSON-encoded shortfall detail for a ``rejected`` row (LLD-07 §4.1), so
    #: a replayed reserve for the same order returns the same 409 body even
    #: after this process restarts. ``NULL`` for every other status.
    reason: Mapped[str | None]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class ReservationLine(Base):
    __tablename__ = "reservation_lines"
    __table_args__ = (CheckConstraint("qty > 0", name="ck_reservation_lines_qty"),)

    reservation_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    sku_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    qty: Mapped[int]


class StockMovement(Base):
    """Append-only ledger (LLD-07 §9's reconciliation sums this against
    ``stock_items``). INV-5's unique constraint is what makes every
    operation in §4 idempotent under replay."""

    __tablename__ = "stock_movements"
    __table_args__ = (
        CheckConstraint(
            "movement_type IN ('reserve','release','expire','commit','restock','adjust')",
            name="ck_stock_movements_type",
        ),
        UniqueConstraint(
            "ref_type", "ref_id", "movement_type", "sku_id", name="uq_stock_movements_ref"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[uuid.UUID]
    sku_id: Mapped[uuid.UUID]
    movement_type: Mapped[str]
    delta_on_hand: Mapped[int]
    delta_reserved: Mapped[int]
    ref_type: Mapped[str]
    ref_id: Mapped[str]
    actor_id: Mapped[uuid.UUID | None]
    created_at: Mapped[datetime]


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
