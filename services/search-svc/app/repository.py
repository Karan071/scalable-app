"""search-svc data access (LLD-05 §7-§8)."""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import Select, delete, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from .models import ProductSearchDoc

#: §8: typo/prefix tolerance threshold for the trigram fallback.
_TRIGRAM_SIMILARITY_THRESHOLD = 0.3
#: §8 "Safety": search queries get a hard statement timeout.
_STATEMENT_TIMEOUT_MS = 1000

#: §8 "Matching": the full-text match, with a trigram fallback for typos
#: and prefixes. One ``text()`` clause, one ``:q`` -- a single bindparams()
#: call covers every occurrence, so there's no risk of two clauses each
#: claiming a same-named parameter.
_MATCH_SQL = "(tsv @@ websearch_to_tsquery('simple', :q) OR (length(:q) >= 3 AND name % :q))"
_SCORE_SQL = "ts_rank_cd(tsv, websearch_to_tsquery('simple', :q)) + 0.5 * similarity(name, :q)"


async def apply_visible_snapshot(
    session: AsyncSession,
    *,
    org_id: UUID,
    product_id: UUID,
    slug: str,
    name: str,
    description_text: str,
    category_ids: list[UUID],
    min_price_minor: int | None,
    max_price_minor: int | None,
    version: int,
) -> None:
    """LLD-05 §7.2: ``product.updated.v1`` with status ``active``. Applied
    only if ``version`` is newer than what's stored -- events can arrive out
    of order across queues and retries, and this makes applying them, in
    any order, converge on the product's latest state."""
    now = datetime.now(UTC)
    stmt = pg_insert(ProductSearchDoc).values(
        org_id=org_id,
        product_id=product_id,
        slug=slug,
        name=name,
        description_text=description_text,
        category_ids=category_ids,
        min_price_minor=min_price_minor,
        max_price_minor=max_price_minor,
        in_stock=True,  # no inventory-svc yet (this service's own open item)
        created_at=now,
        product_version=version,
        indexed_at=now,
        deleted_at=None,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[ProductSearchDoc.org_id, ProductSearchDoc.product_id],
        set_={
            "slug": stmt.excluded.slug,
            "name": stmt.excluded.name,
            "description_text": stmt.excluded.description_text,
            "category_ids": stmt.excluded.category_ids,
            "min_price_minor": stmt.excluded.min_price_minor,
            "max_price_minor": stmt.excluded.max_price_minor,
            "in_stock": stmt.excluded.in_stock,
            "product_version": stmt.excluded.product_version,
            "indexed_at": stmt.excluded.indexed_at,
            "deleted_at": stmt.excluded.deleted_at,
            # created_at is deliberately left alone on update: it's "when
            # first indexed", not "when last touched" (the newest sort).
        },
        where=stmt.excluded.product_version > ProductSearchDoc.product_version,
    )
    await session.execute(stmt)


async def apply_tombstone(
    session: AsyncSession, *, org_id: UUID, product_id: UUID, version: int
) -> None:
    """LLD-05 §7.2: ``product.updated.v1`` (status draft/archived) or
    ``product.deleted.v1``. The row is kept, not removed (a 7-day-retention
    open item), with ``deleted_at`` set -- so a stale, older "active" event
    that arrives after this one is still rejected by the version guard
    rather than resurrecting a deleted/unpublished product."""
    now = datetime.now(UTC)
    stmt = pg_insert(ProductSearchDoc).values(
        org_id=org_id,
        product_id=product_id,
        slug="",
        name="",
        description_text="",
        category_ids=[],
        min_price_minor=None,
        max_price_minor=None,
        in_stock=False,
        created_at=now,
        product_version=version,
        indexed_at=now,
        deleted_at=now,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[ProductSearchDoc.org_id, ProductSearchDoc.product_id],
        set_={
            "product_version": stmt.excluded.product_version,
            "deleted_at": stmt.excluded.deleted_at,
        },
        where=stmt.excluded.product_version > ProductSearchDoc.product_version,
    )
    await session.execute(stmt)


async def delete_stale_after_reindex(
    session: AsyncSession, org_id: UUID, *, seen_product_ids: set[UUID], indexed_before: datetime
) -> int:
    """LLD-05 §9 step 3: a full reindex's cleanup. Deletes for real (not a
    tombstone -- the reindex just proved these products are gone from
    catalog-svc's export, so there's nothing left an out-of-order event
    could race): rows for this org not in ``seen_product_ids`` whose
    ``indexed_at`` predates the reindex job's own start, so a product that
    arrives mid-run isn't mistaken for one that's gone."""
    doc = ProductSearchDoc
    stmt = (
        delete(doc)
        .where(doc.org_id == org_id, doc.indexed_at < indexed_before)
        .returning(doc.product_id)
    )
    if seen_product_ids:
        stmt = stmt.where(doc.product_id.not_in(seen_product_ids))
    deleted = (await session.execute(stmt)).scalars().all()
    return len(deleted)


@dataclass(frozen=True)
class SearchHit:
    product_id: UUID
    slug: str
    name: str
    primary_image_key: str | None
    min_price_minor: int | None
    max_price_minor: int | None
    in_stock: bool
    created_at: datetime


@dataclass(frozen=True)
class SearchParams:
    q: str
    category: UUID | None
    price_min: int | None
    price_max: int | None
    in_stock: bool | None
    sort: str
    limit: int
    #: Keyset cursor for "newest"/"price_*" sorts: (sort_key, after_id).
    cursor: tuple[object, UUID] | None
    #: Capped-offset pagination for "relevance" only (§8's documented trade-off).
    page: int


def _filtered(org_id: UUID, params: SearchParams) -> Select:
    doc = ProductSearchDoc
    stmt = select(doc).where(doc.org_id == org_id, doc.deleted_at.is_(None))
    if params.category is not None:
        stmt = stmt.where(doc.category_ids.contains([params.category]))
    if params.price_min is not None:
        stmt = stmt.where(doc.max_price_minor >= params.price_min)
    if params.price_max is not None:
        stmt = stmt.where(doc.min_price_minor <= params.price_max)
    if params.in_stock is not None:
        stmt = stmt.where(doc.in_stock == params.in_stock)
    if params.q:
        stmt = stmt.where(text(_MATCH_SQL).bindparams(q=params.q))
    return stmt


async def search(session: AsyncSession, org_id: UUID, params: SearchParams) -> list[SearchHit]:
    await session.execute(text(f"SET LOCAL statement_timeout = {_STATEMENT_TIMEOUT_MS}"))
    await session.execute(
        text(f"SET LOCAL pg_trgm.similarity_threshold = {_TRIGRAM_SIMILARITY_THRESHOLD}")
    )

    doc = ProductSearchDoc
    stmt = _filtered(org_id, params)

    if params.sort == "relevance":
        # `TextClause` has no `.desc()`; the direction is part of the SQL text.
        score_desc = text(f"{_SCORE_SQL} DESC").bindparams(q=params.q)
        stmt = stmt.order_by(score_desc, doc.product_id).offset((params.page - 1) * params.limit)
    elif params.sort == "newest":
        if params.cursor is not None:
            after_created_at, after_id = params.cursor
            stmt = stmt.where(
                (doc.created_at < after_created_at)
                | ((doc.created_at == after_created_at) & (doc.product_id < after_id))
            )
        stmt = stmt.order_by(doc.created_at.desc(), doc.product_id.desc())
    else:  # price_asc | price_desc
        ascending = params.sort == "price_asc"
        price_col = doc.min_price_minor
        if params.cursor is not None:
            after_price, after_id = params.cursor
            if ascending:
                stmt = stmt.where(
                    (price_col > after_price)
                    | ((price_col == after_price) & (doc.product_id > after_id))
                )
            else:
                stmt = stmt.where(
                    (price_col < after_price)
                    | ((price_col == after_price) & (doc.product_id < after_id))
                )
        order = price_col.asc() if ascending else price_col.desc()
        order_id = doc.product_id.asc() if ascending else doc.product_id.desc()
        stmt = stmt.order_by(order, order_id)

    stmt = stmt.limit(params.limit)
    rows = (await session.execute(stmt)).scalars().all()
    return [
        SearchHit(
            product_id=r.product_id,
            slug=r.slug,
            name=r.name,
            primary_image_key=r.primary_image_key,
            min_price_minor=r.min_price_minor,
            max_price_minor=r.max_price_minor,
            in_stock=r.in_stock,
            created_at=r.created_at,
        )
        for r in rows
    ]
