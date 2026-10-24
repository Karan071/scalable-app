"""search-svc business logic (LLD-05 §7-§9): applying catalog events to the
index, and validating + running a search query.
"""

import base64
import re
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sf_db import InvalidCursor
from sf_db import decode_cursor as _decode_cursor
from sf_db import encode_cursor as _encode_cursor
from sqlalchemy.ext.asyncio import AsyncSession

from . import repository

_MAX_Q_LENGTH = 100
_DEFAULT_LIMIT, _MAX_LIMIT = 24, 48
_MAX_PAGE = 50
_SORTS = frozenset({"relevance", "newest", "price_asc", "price_desc"})
#: Strips C0/C1 control characters (LLD-05 §8).
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")


class SearchValidationError(Exception):
    """A query the client must fix -- always a 400 (LLD-05 §13), not a 422:
    this is the documented contract for this one endpoint."""


def cursor_secret(settings_secret_b64: str) -> bytes:
    return base64.b64decode(settings_secret_b64)


# --- applying catalog events (LLD-05 §7.2) --------------------------------------


async def apply_product_snapshot(
    session: AsyncSession, data: dict, *, deleted: bool = False
) -> None:
    """The one place that decides upsert vs. tombstone from a product
    snapshot (catalog-svc's own ``_product_snapshot_data`` shape: the same
    dict shows up as an event's ``data`` *and* as one item of the export
    endpoint's ``items``). Both the live consumer and the reindex CLI
    (LLD-05 §9 step 2: "the same version guard") call this exact function,
    so there is exactly one upsert/tombstone code path to get right.

    ``deleted=True`` is for a bare ``product.deleted.v1`` (no display
    fields, only the id) -- catalog-svc doesn't publish that event yet (no
    hard-delete route exists), so this is forward-compatible, not exercised
    by anything today.
    """
    org_id = UUID(data["org_id"])
    product_id = UUID(data["product_id"])
    version = int(data["version"])

    if deleted or data.get("status") in ("draft", "archived"):
        await repository.apply_tombstone(
            session, org_id=org_id, product_id=product_id, version=version
        )
        return

    skus = data.get("skus", [])
    active_prices = [s["price_minor"] for s in skus if s.get("status") == "active"]
    category_ids = [UUID(data["category_id"])] if data.get("category_id") else []
    await repository.apply_visible_snapshot(
        session,
        org_id=org_id,
        product_id=product_id,
        slug=data["slug"],
        name=data["name"],
        description_text=data.get("description_md", ""),
        category_ids=category_ids,
        min_price_minor=min(active_prices) if active_prices else None,
        max_price_minor=max(active_prices) if active_prices else None,
        version=version,
    )


async def apply_product_event(session: AsyncSession, envelope: dict) -> None:
    """Dispatched for both ``product.updated.v1`` and ``product.deleted.v1``
    (event_handlers.DISPATCH). The envelope's own ``type``/``sfaggregateversion``
    aren't part of catalog-svc's snapshot dict, so they're folded in here
    before delegating to ``apply_product_snapshot``."""
    data = {**envelope["data"], "version": envelope["sfaggregateversion"]}
    await apply_product_snapshot(session, data, deleted=envelope["type"] == "product.deleted")


# --- query validation and execution (LLD-05 §8) ---------------------------------


@dataclass(frozen=True)
class RawSearchRequest:
    q: str | None
    category: UUID | None
    price_min: int | None
    price_max: int | None
    in_stock: bool | None
    sort: str | None
    limit: int
    cursor: str | None
    page: int | None


@dataclass(frozen=True)
class SearchResult:
    hits: list[repository.SearchHit]
    next_cursor: str | None


def _clean_q(q: str | None) -> str:
    if q is None:
        return ""
    cleaned = _CONTROL_CHARS.sub("", q).strip()
    if len(cleaned) > _MAX_Q_LENGTH:
        raise SearchValidationError(f"q must be at most {_MAX_Q_LENGTH} characters")
    return cleaned


async def run_search(
    session: AsyncSession, org_id: UUID, request: RawSearchRequest, *, secret_b64: str
) -> SearchResult:
    q = _clean_q(request.q)
    sort = request.sort or ("relevance" if q else "newest")
    if sort not in _SORTS:
        raise SearchValidationError(f"sort must be one of {sorted(_SORTS)}")
    if not (1 <= request.limit <= _MAX_LIMIT):
        raise SearchValidationError(f"limit must be between 1 and {_MAX_LIMIT}")
    if request.price_min is not None and request.price_min < 0:
        raise SearchValidationError("price_min must be >= 0")
    if request.price_max is not None and request.price_max < 0:
        raise SearchValidationError("price_max must be >= 0")
    if (
        request.price_min is not None
        and request.price_max is not None
        and request.price_min > request.price_max
    ):
        raise SearchValidationError("price_min must be <= price_max")

    secret = cursor_secret(secret_b64)
    page = request.page or 1
    if sort == "relevance":
        if request.cursor is not None:
            raise SearchValidationError("cursor is only valid for newest/price_* sorts")
        if not (1 <= page <= _MAX_PAGE):
            raise SearchValidationError(f"page must be between 1 and {_MAX_PAGE}")
        cursor: tuple[object, UUID] | None = None
    else:
        if request.page is not None:
            raise SearchValidationError("page is only valid for the relevance sort")
        try:
            cursor = (
                _decode_keyset_cursor(secret, request.cursor, sort=sort) if request.cursor else None
            )
        except InvalidCursor as exc:
            raise SearchValidationError("invalid cursor") from exc

    params = repository.SearchParams(
        q=q,
        category=request.category,
        price_min=request.price_min,
        price_max=request.price_max,
        in_stock=request.in_stock,
        sort=sort,
        limit=request.limit,
        cursor=cursor,
        page=page,
    )
    hits = await repository.search(session, org_id, params)

    next_cursor = None
    if sort != "relevance" and len(hits) == request.limit:
        last = hits[-1]
        sort_key = last.created_at.isoformat() if sort == "newest" else (last.min_price_minor or 0)
        next_cursor = _encode_cursor(secret, sort_key, last.product_id)
    return SearchResult(hits=hits, next_cursor=next_cursor)


def _decode_keyset_cursor(secret: bytes, cursor: str, *, sort: str) -> tuple[object, UUID]:
    sort_key, after_id = _decode_cursor(secret, cursor)
    if sort == "newest":
        return datetime.fromisoformat(str(sort_key)), after_id
    return int(sort_key), after_id
