"""SQLAlchemy models for the ``catalog`` schema (LLD-05 §2) and its outbox (LLD-08 §5.1).

Deferred to a later slice (named in LLD-05 §14/this service's own open
items): product images (object-storage integration) and the full-text
search index (search-svc, a separate service/schema).
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Text, TypeDecorator, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class LTree(TypeDecorator):
    """``ltree`` (the Postgres extension init-schemas.sh enables), with no
    native SQLAlchemy type in this version. Values are plain dotted-label
    strings (``"a.b.c"``) on the Python side; Postgres does the comparisons."""

    impl = Text
    cache_ok = True

    def get_col_spec(self, **kw: object) -> str:
        return "LTREE"


class Base(DeclarativeBase):
    # Every migration column is timestamptz (LLD-13 §6: UTC everywhere); see
    # users-svc/app/models.py for why this mapping is needed.
    type_annotation_map = {datetime: DateTime(timezone=True)}


class Category(Base):
    __tablename__ = "categories"
    __table_args__ = (
        UniqueConstraint("org_id", "parent_id", "slug", name="uq_categories_org_parent_slug"),
        CheckConstraint("nlevel(path) <= 4", name="ck_categories_max_depth"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    org_id: Mapped[uuid.UUID]
    parent_id: Mapped[uuid.UUID | None]
    path: Mapped[str] = mapped_column(LTree)
    name: Mapped[str]
    slug: Mapped[str]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("org_id", "slug", name="uq_products_org_slug"),
        CheckConstraint(
            "status IN ('draft','active','archived')", name="ck_products_status"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    org_id: Mapped[uuid.UUID]
    slug: Mapped[str]
    name: Mapped[str]
    description_md: Mapped[str]
    description_html: Mapped[str]
    status: Mapped[str]
    category_id: Mapped[uuid.UUID | None]
    version: Mapped[int]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class Sku(Base):
    __tablename__ = "skus"
    __table_args__ = (
        UniqueConstraint("org_id", "sku_code", name="uq_skus_org_code"),
        CheckConstraint("price_minor >= 0", name="ck_skus_price_minor"),
        CheckConstraint(
            "compare_at_minor IS NULL OR compare_at_minor >= price_minor",
            name="ck_skus_compare_at",
        ),
        CheckConstraint("status IN ('active','archived')", name="ck_skus_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    org_id: Mapped[uuid.UUID]
    product_id: Mapped[uuid.UUID]
    sku_code: Mapped[str]
    attributes: Mapped[dict] = mapped_column(JSONB, default=dict)
    price_minor: Mapped[int]
    compare_at_minor: Mapped[int | None]
    status: Mapped[str]
    version: Mapped[int]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class Promotion(Base):
    __tablename__ = "promotions"
    __table_args__ = (
        UniqueConstraint("org_id", "code", name="uq_promotions_org_code"),
        CheckConstraint("kind IN ('percent','fixed')", name="ck_promotions_kind"),
        CheckConstraint(
            "(kind = 'percent' AND value BETWEEN 1 AND 90) "
            "OR (kind = 'fixed' AND value > 0)",
            name="ck_promotions_value",
        ),
        CheckConstraint("status IN ('active','disabled')", name="ck_promotions_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    org_id: Mapped[uuid.UUID]
    code: Mapped[str]
    kind: Mapped[str]
    value: Mapped[int]
    min_subtotal_minor: Mapped[int]
    starts_at: Mapped[datetime]
    ends_at: Mapped[datetime]
    status: Mapped[str]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class PriceHistory(Base):
    """Append-only (LLD-05 §2): one row per SKU price change, for audit and disputes."""

    __tablename__ = "price_history"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    org_id: Mapped[uuid.UUID]
    sku_id: Mapped[uuid.UUID]
    old_price_minor: Mapped[int]
    new_price_minor: Mapped[int]
    actor_id: Mapped[uuid.UUID]
    occurred_at: Mapped[datetime]


class StorePricingSettings(Base):
    """Projection from ``organization.*.v1`` events (LLD-05 §2): the only
    way catalog-svc learns an org's currency/shipping/tax/active status,
    so pricing a quote never calls orgs-svc synchronously."""

    __tablename__ = "store_pricing_settings"

    org_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    status: Mapped[str]
    currency: Mapped[str]
    shipping_flat_minor: Mapped[int]
    tax_rate_bps: Mapped[int]
    #: The source event's aggregate_version — guards against an out-of-order
    #: apply (LLD-08 §7: events can arrive out of order across retries).
    source_version: Mapped[int]
    updated_at: Mapped[datetime]


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
