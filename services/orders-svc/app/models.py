"""SQLAlchemy models for the ``orders`` schema (LLD-06 §3.1, §4, §5.2) and
its outbox (LLD-08 §5.1).
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


#: LLD-06 §5.2's state diagram. Renamed from the LLD's literal
#: "REreserving" to the same SCREAMING_SNAKE_CASE as every other state --
#: cosmetic only, same token meaning "re-reserve after the original
#: reservation expired mid-saga".
SAGA_STATES = (
    "RESERVING",
    "RESERVED",
    "AUTHORIZING",
    "AUTH_PENDING",
    "AUTHORIZED",
    "COMMITTING",
    "CAPTURING",
    "RE_RESERVING",
    "RESTOCKING",
    "VOIDING",
    "RELEASING",
    "COMPLETED",
    "CANCELLED",
)
SAGA_TERMINAL_STATES = ("COMPLETED", "CANCELLED")

ORDER_STATUSES = (
    "pending",
    "confirmed",
    "processing",
    "shipped",
    "delivered",
    "cancelled",
    "refunded",
)
CANCEL_REASONS = (
    "out_of_stock",
    "payment_declined",
    "payment_timeout",
    "reservation_expired",
    "capture_failed",
    "customer_request",
    "staff_request",
)
#: LLD-06 §7.2's payment state machine, plus "none" before any attempt
#: exists -- this is orders-svc's own *projection* of that state, not the
#: payment_attempts row itself (payments-svc owns that; doesn't exist yet).
PAYMENT_STATUSES = (
    "none",
    "authorizing",
    "authorized",
    "declined",
    "unknown",
    "capturing",
    "captured",
    "voided",
    "refunding",
    "refunded",
)


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("customer_id", "idempotency_key", name="uq_orders_customer_idem_key"),
        CheckConstraint(f"status IN {ORDER_STATUSES!r}", name="ck_orders_status"),
        CheckConstraint(f"payment_status IN {PAYMENT_STATUSES!r}", name="ck_orders_payment_status"),
        CheckConstraint(
            f"cancel_reason IS NULL OR cancel_reason IN {CANCEL_REASONS!r}",
            name="ck_orders_cancel_reason",
        ),
        CheckConstraint(
            "total_minor = subtotal_minor - discount_minor + shipping_minor + tax_minor",
            name="ck_orders_total",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    org_id: Mapped[uuid.UUID]
    customer_id: Mapped[uuid.UUID]
    status: Mapped[str]
    payment_status: Mapped[str]
    cart_id: Mapped[uuid.UUID]
    cart_version: Mapped[int]
    idempotency_key: Mapped[uuid.UUID]
    currency: Mapped[str]
    subtotal_minor: Mapped[int]
    discount_minor: Mapped[int]
    shipping_minor: Mapped[int]
    tax_minor: Mapped[int]
    total_minor: Mapped[int]
    promo_code: Mapped[str | None]
    pricing_fingerprint: Mapped[str]
    shipping_address: Mapped[dict] = mapped_column(JSONB)
    cancel_reason: Mapped[str | None]
    version: Mapped[int]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class OrderLine(Base):
    __tablename__ = "order_lines"
    __table_args__ = (
        CheckConstraint("line_total_minor = unit_price_minor * qty", name="ck_order_lines_total"),
    )

    order_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    line_no: Mapped[int] = mapped_column(primary_key=True)
    sku_id: Mapped[uuid.UUID]
    product_id: Mapped[uuid.UUID]
    name_snapshot: Mapped[str]
    unit_price_minor: Mapped[int]
    qty: Mapped[int]
    line_total_minor: Mapped[int]


class OrderStatusHistory(Base):
    __tablename__ = "order_status_history"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    order_id: Mapped[uuid.UUID]
    from_status: Mapped[str | None]
    to_status: Mapped[str]
    actor_type: Mapped[str]
    actor_id: Mapped[str]
    reason: Mapped[str | None]
    at: Mapped[datetime]


class CheckoutSaga(Base):
    __tablename__ = "checkout_sagas"
    __table_args__ = (CheckConstraint(f"state IN {SAGA_STATES!r}", name="ck_checkout_sagas_state"),)

    order_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    state: Mapped[str]
    reservation_id: Mapped[uuid.UUID | None]
    payment_id: Mapped[uuid.UUID | None]
    attempts: Mapped[int]
    next_action_at: Mapped[datetime | None]
    deadline_at: Mapped[datetime]
    last_error: Mapped[str | None]
    version: Mapped[int]
    updated_at: Mapped[datetime]


class IdempotencyKey(Base):
    """LLD-06 §4. Generic across every idempotent write this service does
    (``checkout``, ``order_cancel``, ``refund``), discriminated by ``scope``."""

    __tablename__ = "idempotency_keys"
    __table_args__ = (
        CheckConstraint("status IN ('in_progress','completed')", name="ck_idempotency_keys_status"),
    )

    scope: Mapped[str] = mapped_column(primary_key=True)
    principal_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    key: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    request_hash: Mapped[str]
    status: Mapped[str]
    locked_until: Mapped[datetime]
    resource_id: Mapped[uuid.UUID | None]
    response_status: Mapped[int | None]
    response_body: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime]
    expires_at: Mapped[datetime]


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
