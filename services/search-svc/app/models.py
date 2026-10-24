"""SQLAlchemy model for the ``search`` schema (LLD-05 §7.1).

``tsv`` is deliberately **not** mapped here: it's a generated Postgres
column (``GENERATED ALWAYS AS ... STORED`` in the migration), computed from
``name``/``description_text`` by Postgres itself, never set from Python.
Every query that touches it (``repository.py``) does so in raw SQL.

Deferred (this service's own open items, LLD-14 §4 / LLD-05 §14): the
``sku_availability`` supporting table and the ``inventory.availability_changed.v1``
consumer that would feed it — inventory-svc doesn't exist yet, so `in_stock`
is always `true`.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime
from sqlalchemy.dialects.postgresql import ARRAY as PG_ARRAY
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    # Every migration column is timestamptz (LLD-13 §6: UTC everywhere); see
    # users-svc/app/models.py for why this mapping is needed.
    type_annotation_map = {datetime: DateTime(timezone=True)}


class ProductSearchDoc(Base):
    __tablename__ = "product_search_docs"

    org_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    product_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    slug: Mapped[str]
    name: Mapped[str]
    description_text: Mapped[str]
    primary_image_key: Mapped[str | None]
    category_ids: Mapped[list[uuid.UUID]] = mapped_column(PG_ARRAY(PG_UUID(as_uuid=True)))
    min_price_minor: Mapped[int | None]
    max_price_minor: Mapped[int | None]
    in_stock: Mapped[bool]
    created_at: Mapped[datetime]
    product_version: Mapped[int]
    indexed_at: Mapped[datetime]
    #: Non-null = tombstoned (LLD-05 §7.2): kept, not deleted, so a stale
    #: out-of-order "active" event older than this can't resurrect it.
    deleted_at: Mapped[datetime | None]
