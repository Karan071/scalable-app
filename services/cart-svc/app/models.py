"""SQLAlchemy models for the ``cart`` schema (LLD-06 §2).

No outbox here: cart-svc publishes nothing in this slice (it has no event
any other service currently needs) -- unlike every other service in this
build, there's no ``outbox`` table.
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    # Every migration column is timestamptz (LLD-13 §6: UTC everywhere); see
    # users-svc/app/models.py for why this mapping is needed.
    type_annotation_map = {datetime: DateTime(timezone=True)}


class Cart(Base):
    __tablename__ = "carts"
    __table_args__ = (
        CheckConstraint("status IN ('active','checked_out','abandoned')", name="ck_carts_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    customer_id: Mapped[uuid.UUID]
    org_id: Mapped[uuid.UUID]
    status: Mapped[str]
    version: Mapped[int]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
    expires_at: Mapped[datetime]


class CartItem(Base):
    __tablename__ = "cart_items"
    __table_args__ = (CheckConstraint("qty BETWEEN 1 AND 99", name="ck_cart_items_qty"),)

    cart_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    sku_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    product_id: Mapped[uuid.UUID]
    qty: Mapped[int]
    added_at: Mapped[datetime]
