"""catalog-svc HTTP routes (LLD-05 §4). Every route declares `Authorize(...)`,
`Internal(...)`, or `Public(...)` (LLD-02 §6); `org_domain_from_path()`
resolves the org domain from the URL path, never the body (FR-ORG-05).

Deferred (this service's own open items, LLD-05 §14): product images. The
full-text search index lives in search-svc, which consumes this service's
``product.*`` events and its export endpoint below (LLD-05 §9) rather than
being built here. Listing/browse (LLD-05 §6's `/v1/stores/{orgId}/search`)
is search-svc's own public route, not this service's -- the public routes
here are only the single-product detail read and the category tree, the
two LLD-05 §6 reads search-svc can't itself serve.
"""

import base64
import json
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sf_authz import Authorize, Internal, Principal, Public, org_domain_from_path
from sf_common import BulkheadFullError, CacheClient
from sqlalchemy.ext.asyncio import AsyncSession

from . import pricing, repository, service
from .db import get_product_cache, get_session
from .models import Category, Product, Promotion, Sku

router = APIRouter()

_org_domain = org_domain_from_path()

require_read_product = Authorize("product", "read", domain=_org_domain)
require_create_product = Authorize("product", "create", domain=_org_domain)
require_update_product = Authorize("product", "update", domain=_org_domain)
require_storefront_read = Public(rate_class="storefront")

require_read_category = Authorize("category", "read", domain=_org_domain)
require_create_category = Authorize("category", "create", domain=_org_domain)
require_update_category = Authorize("category", "update", domain=_org_domain)
require_delete_category = Authorize("category", "delete", domain=_org_domain)

require_read_promotion = Authorize("promotion", "read", domain=_org_domain)
require_create_promotion = Authorize("promotion", "create", domain=_org_domain)
require_update_promotion = Authorize("promotion", "update", domain=_org_domain)
require_delete_promotion = Authorize("promotion", "delete", domain=_org_domain)

require_quote_scope = Internal("catalog:quotes")
require_export_scope = Internal("catalog:export")
require_sku_read_scope = Internal("catalog:read_sku")

_DEFAULT_LIMIT = 24
_MAX_LIMIT = 48
_MAX_STOREFRONT_SKU_LOOKUP = 50


def _sf_uid(principal: Principal) -> UUID:
    raw = principal.claim("sf_uid")
    if raw is None:
        raise HTTPException(
            status_code=403, detail="token has no sf_uid claim yet; refresh required"
        )
    return UUID(str(raw))


def _decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        raw = json.loads(base64.urlsafe_b64decode(cursor + "==").decode())
        return datetime.fromisoformat(raw["updated_at"]), UUID(raw["id"])
    except Exception as exc:
        raise HTTPException(status_code=422, detail="invalid cursor") from exc


def _encode_cursor(updated_at: datetime, product_id: UUID) -> str:
    raw = json.dumps({"updated_at": updated_at.isoformat(), "id": str(product_id)})
    return base64.urlsafe_b64encode(raw.encode()).rstrip(b"=").decode()


# --- categories --------------------------------------------------------------


class CategoryResponse(BaseModel):
    id: UUID
    org_id: UUID
    parent_id: UUID | None
    path: str
    name: str
    slug: str


def _category_out(c: Category) -> CategoryResponse:
    return CategoryResponse(
        id=c.id, org_id=c.org_id, parent_id=c.parent_id, path=str(c.path), name=c.name, slug=c.slug
    )


class CreateCategoryRequest(BaseModel):
    model_config = {"extra": "forbid"}
    name: str
    slug: str
    parent_id: UUID | None = None


@router.get("/v1/orgs/{org_id}/categories", response_model=list[CategoryResponse])
async def list_categories(
    org_id: UUID,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_read_category),
) -> list[CategoryResponse]:
    return [_category_out(c) for c in await repository.list_categories(session, org_id)]


@router.post("/v1/orgs/{org_id}/categories", response_model=CategoryResponse)
async def create_category(
    org_id: UUID,
    body: CreateCategoryRequest,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_create_category),
) -> CategoryResponse:
    try:
        category = await service.create_category(
            session, org_id=org_id, name=body.name, slug=body.slug, parent_id=body.parent_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except service.NotFoundError as exc:
        raise HTTPException(status_code=404, detail="parent category not found") from exc
    return _category_out(category)


class UpdateCategoryRequest(BaseModel):
    model_config = {"extra": "forbid"}
    name: str


@router.patch("/v1/orgs/{org_id}/categories/{category_id}", response_model=CategoryResponse)
async def update_category(
    org_id: UUID,
    category_id: UUID,
    body: UpdateCategoryRequest,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_update_category),
) -> CategoryResponse:
    try:
        category = await service.update_category(
            session, category_id, org_id=org_id, name=body.name
        )
    except service.NotFoundError as exc:
        raise HTTPException(status_code=404) from exc
    return _category_out(category)


@router.delete("/v1/orgs/{org_id}/categories/{category_id}", response_model=None)
async def delete_category(
    org_id: UUID,
    category_id: UUID,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_delete_category),
) -> dict[str, str]:
    try:
        await service.delete_category(session, category_id, org_id=org_id)
    except service.NotFoundError as exc:
        raise HTTPException(status_code=404) from exc
    except service.HasChildrenError as exc:
        raise HTTPException(
            status_code=409, detail="category has subcategories or products"
        ) from exc
    return {"status": "deleted"}


# --- storefront (public) reads, LLD-05 §6 -----------------------------------


@router.get("/v1/stores/{org_id}/categories", response_model=list[CategoryResponse])
async def list_storefront_categories(
    org_id: UUID,
    session: AsyncSession = Depends(get_session),
    _: None = Depends(require_storefront_read),
) -> list[CategoryResponse]:
    return [_category_out(c) for c in await repository.list_categories(session, org_id)]


class StorefrontSkuOut(BaseModel):
    sku_id: UUID
    product_id: UUID
    product_name: str
    price_minor: int


class StorefrontSkuBatchResponse(BaseModel):
    items: list[StorefrontSkuOut]


@router.get("/v1/stores/{org_id}/skus", response_model=StorefrontSkuBatchResponse)
async def list_storefront_skus(
    org_id: UUID,
    sku_ids: list[UUID] = Query(...),
    session: AsyncSession = Depends(get_session),
    _: None = Depends(require_storefront_read),
) -> StorefrontSkuBatchResponse:
    """Name/price for a batch of SKUs, keyed by id -- the cart page's only
    way to show what a line item actually is, since cart-svc stores only
    `sku_id`/`qty` (LLD-06 §2). Mirrors inventory-svc's public
    `:sku_ids=` availability batching. The browser never sees the
    `catalog:quotes`/`catalog:read_sku` internal endpoints -- this is the
    public-safe subset of the same join (no sku/product status, which
    would leak draft/archived state)."""
    if len(sku_ids) > _MAX_STOREFRONT_SKU_LOOKUP:
        raise HTTPException(status_code=422, detail="too many sku_ids")
    rows = await repository.quote_inputs(session, org_id, sku_ids)
    return StorefrontSkuBatchResponse(
        items=[
            StorefrontSkuOut(
                sku_id=row.sku_id,
                product_id=row.product_id,
                product_name=row.product_name,
                price_minor=row.price_minor,
            )
            for row in rows.values()
        ]
    )


# --- products ------------------------------------------------------------------


class SkuOut(BaseModel):
    id: UUID
    sku_code: str
    attributes: dict[str, object]
    price_minor: int
    compare_at_minor: int | None
    status: str
    version: int


def _sku_out(s: Sku) -> SkuOut:
    return SkuOut(
        id=s.id,
        sku_code=s.sku_code,
        attributes=s.attributes,
        price_minor=s.price_minor,
        compare_at_minor=s.compare_at_minor,
        status=s.status,
        version=s.version,
    )


class ProductResponse(BaseModel):
    id: UUID
    org_id: UUID
    slug: str
    name: str
    description_md: str
    description_html: str
    status: str
    category_id: UUID | None
    version: int
    skus: list[SkuOut] = []


def _product_out(p: Product, skus: list[Sku] | None = None) -> ProductResponse:
    return ProductResponse(
        id=p.id,
        org_id=p.org_id,
        slug=p.slug,
        name=p.name,
        description_md=p.description_md,
        description_html=p.description_html,
        status=p.status,
        category_id=p.category_id,
        version=p.version,
        skus=[_sku_out(s) for s in skus] if skus is not None else [],
    )


class CreateProductRequest(BaseModel):
    model_config = {"extra": "forbid"}
    slug: str
    name: str
    description_md: str = ""
    category_id: UUID | None = None


class ProductPage(BaseModel):
    items: list[ProductResponse]
    next_cursor: str | None


@router.get("/v1/orgs/{org_id}/products", response_model=ProductPage)
async def list_products(
    org_id: UUID,
    status: str | None = Query(default=None),
    category: UUID | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_read_product),
) -> ProductPage:
    decoded = _decode_cursor(cursor) if cursor else None
    products = await repository.list_products(
        session, org_id, status=status, category_id=category, cursor=decoded, limit=limit
    )
    next_cursor = (
        _encode_cursor(products[-1].updated_at, products[-1].id) if len(products) == limit else None
    )
    return ProductPage(items=[_product_out(p) for p in products], next_cursor=next_cursor)


@router.post("/v1/orgs/{org_id}/products", response_model=ProductResponse)
async def create_product(
    org_id: UUID,
    body: CreateProductRequest,
    response: Response,
    principal: Principal = Depends(require_create_product),
    session: AsyncSession = Depends(get_session),
) -> ProductResponse:
    try:
        product = await service.create_product(
            session,
            org_id=org_id,
            actor_id=_sf_uid(principal),
            slug=body.slug,
            name=body.name,
            description_md=body.description_md,
            category_id=body.category_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except service.NotFoundError as exc:
        raise HTTPException(status_code=404, detail="category not found") from exc
    response.headers["etag"] = f'"v{product.version}"'
    return _product_out(product)


@router.get("/v1/orgs/{org_id}/products/{product_id}", response_model=ProductResponse)
async def get_product(
    org_id: UUID,
    product_id: UUID,
    response: Response,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_read_product),
) -> ProductResponse:
    product = await repository.get_product(session, product_id, org_id=org_id)
    if product is None:
        raise HTTPException(status_code=404)
    skus = await repository.list_skus(session, product_id, org_id=org_id)
    response.headers["etag"] = f'"v{product.version}"'
    return _product_out(product, skus)


def _product_cache_key(org_id: UUID, slug: str) -> str:
    return f"cat:v1:prod:{org_id}:{slug}"


@router.get("/v1/stores/{org_id}/products/{slug}", response_model=ProductResponse)
async def get_storefront_product(
    org_id: UUID,
    slug: str,
    session: AsyncSession = Depends(get_session),
    product_cache: CacheClient = Depends(get_product_cache),
    _: None = Depends(require_storefront_read),
) -> ProductResponse:
    """LLD-05 §6: only `active` products are visible publicly. Images are
    deferred (no images subsystem exists yet, this service's own §14 item),
    so this is product + SKUs, not product + SKUs + ready images.

    Cached per LLD-09 §6.1 (`cat:v1:prod:{org}:{slug}`, 300s TTL ± jitter,
    invalidated by `update_product`/`publish_product`/`archive_product`
    below). The same row's negative-lookup cache (`cat:v1:prod404:*`,
    "delete on create") isn't built -- a 404 here always costs a real DB
    query, a documented gap, not a correctness issue. A SKU-only edit
    (price, stock) isn't invalidated either (only a product-level field
    is) -- bounded by this cache's own 300s TTL, same staleness class as
    any of this table's other rows.
    """

    async def load() -> tuple[int, dict[str, Any]]:
        product = await repository.get_active_product_by_slug(session, org_id, slug)
        if product is None:
            raise HTTPException(status_code=404)
        skus = await repository.list_skus(session, product.id, org_id=org_id)
        return product.version, _product_out(product, skus).model_dump(mode="json")

    try:
        cached = await product_cache.get_or_load(
            _product_cache_key(org_id, slug), loader=load, ttl_s=300.0
        )
    except BulkheadFullError as exc:
        raise HTTPException(
            status_code=503, detail="cache-busy", headers={"retry-after": "1"}
        ) from exc
    return ProductResponse.model_validate(cached)


class UpdateProductRequest(BaseModel):
    model_config = {"extra": "forbid"}
    name: str | None = None
    description_md: str | None = None
    category_id: UUID | None = None
    category_id_set: bool = False


def _if_match_version(if_match: str) -> int:
    try:
        return int(if_match.strip('"').removeprefix("v"))
    except ValueError as exc:
        raise HTTPException(status_code=428, detail="invalid If-Match") from exc


@router.patch("/v1/orgs/{org_id}/products/{product_id}", response_model=ProductResponse)
async def update_product(
    org_id: UUID,
    product_id: UUID,
    body: UpdateProductRequest,
    response: Response,
    background_tasks: BackgroundTasks,
    principal: Principal = Depends(require_update_product),
    if_match: str = Header(..., alias="If-Match"),
    session: AsyncSession = Depends(get_session),
    product_cache: CacheClient = Depends(get_product_cache),
) -> ProductResponse:
    try:
        product = await service.update_product(
            session,
            product_id,
            org_id=org_id,
            actor_id=_sf_uid(principal),
            expected_version=_if_match_version(if_match),
            name=body.name,
            description_md=body.description_md,
            category_id=body.category_id,
            category_id_set=body.category_id_set,
        )
    except service.VersionConflictError as exc:
        current = await repository.get_product(session, product_id, org_id=org_id)
        if current is None:
            raise HTTPException(status_code=404) from exc
        raise HTTPException(
            status_code=412,
            detail="version conflict, refetch and retry",
            headers={"etag": f'"v{current.version}"'},
        ) from exc
    except service.NotFoundError as exc:
        raise HTTPException(status_code=404, detail="category not found") from exc
    response.headers["etag"] = f'"v{product.version}"'
    # After the response (and this request's own commit, via get_session's
    # dependency teardown) -- never inline, get_product_cache's own
    # docstring has the "why".
    background_tasks.add_task(product_cache.invalidate, _product_cache_key(org_id, product.slug))
    return _product_out(product)


class TransitionRequest(BaseModel):
    model_config = {"extra": "forbid"}


async def _transition(
    org_id: UUID,
    product_id: UUID,
    *,
    action: str,
    principal: Principal,
    if_match: str,
    session: AsyncSession,
    background_tasks: BackgroundTasks,
    product_cache: CacheClient,
) -> ProductResponse:
    try:
        product = await service.transition_product(
            session,
            product_id,
            org_id=org_id,
            actor_id=_sf_uid(principal),
            expected_version=_if_match_version(if_match),
            action=action,
        )
    except service.NotFoundError as exc:
        raise HTTPException(status_code=404) from exc
    except service.VersionConflictError as exc:
        current = await repository.get_product(session, product_id, org_id=org_id)
        headers = {"etag": f'"v{current.version}"'} if current else None
        raise HTTPException(
            status_code=412, detail="version conflict, refetch and retry", headers=headers
        ) from exc
    except service.InvalidTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    # publish/archive are exactly the transitions that flip storefront
    # visibility (LLD-05 §6), so this cache is never allowed to lag behind
    # one past its TTL -- see get_product_cache's own docstring re: why
    # this is a background task, not an inline call.
    background_tasks.add_task(product_cache.invalidate, _product_cache_key(org_id, product.slug))
    return _product_out(product)


@router.post("/v1/orgs/{org_id}/products/{product_id}:publish", response_model=ProductResponse)
async def publish_product(
    org_id: UUID,
    product_id: UUID,
    body: TransitionRequest,
    background_tasks: BackgroundTasks,
    principal: Principal = Depends(require_update_product),
    if_match: str = Header(..., alias="If-Match"),
    session: AsyncSession = Depends(get_session),
    product_cache: CacheClient = Depends(get_product_cache),
) -> ProductResponse:
    return await _transition(
        org_id,
        product_id,
        action="publish",
        principal=principal,
        if_match=if_match,
        session=session,
        background_tasks=background_tasks,
        product_cache=product_cache,
    )


@router.post("/v1/orgs/{org_id}/products/{product_id}:archive", response_model=ProductResponse)
async def archive_product(
    org_id: UUID,
    product_id: UUID,
    body: TransitionRequest,
    background_tasks: BackgroundTasks,
    principal: Principal = Depends(require_update_product),
    if_match: str = Header(..., alias="If-Match"),
    session: AsyncSession = Depends(get_session),
    product_cache: CacheClient = Depends(get_product_cache),
) -> ProductResponse:
    return await _transition(
        org_id,
        product_id,
        action="archive",
        principal=principal,
        if_match=if_match,
        session=session,
        background_tasks=background_tasks,
        product_cache=product_cache,
    )


# --- SKUs ----------------------------------------------------------------------


class PutSkuRequest(BaseModel):
    model_config = {"extra": "forbid"}
    sku_code: str
    attributes: dict[str, object] = Field(default_factory=dict)
    price_minor: int = Field(ge=0)
    compare_at_minor: int | None = Field(default=None, ge=0)


@router.put("/v1/orgs/{org_id}/products/{product_id}/skus/{sku_id}", response_model=SkuOut)
async def put_sku(
    org_id: UUID,
    product_id: UUID,
    sku_id: UUID,
    body: PutSkuRequest,
    response: Response,
    principal: Principal = Depends(require_update_product),
    if_match: str | None = Header(default=None, alias="If-Match"),
    session: AsyncSession = Depends(get_session),
) -> SkuOut:
    try:
        sku = await service.put_sku(
            session,
            org_id=org_id,
            actor_id=_sf_uid(principal),
            product_id=product_id,
            sku_id=sku_id,
            if_match=_if_match_version(if_match) if if_match else None,
            sku_code=body.sku_code,
            attributes=body.attributes,
            price_minor=body.price_minor,
            compare_at_minor=body.compare_at_minor,
        )
    except service.NotFoundError as exc:
        raise HTTPException(status_code=404) from exc
    except service.MissingIfMatchError as exc:
        raise HTTPException(status_code=428, detail="If-Match required to update a sku") from exc
    except service.VersionConflictError as exc:
        current = await repository.get_sku(session, sku_id, org_id=org_id)
        headers = {"etag": f'"v{current.version}"'} if current else None
        raise HTTPException(
            status_code=412, detail="version conflict, refetch and retry", headers=headers
        ) from exc
    response.headers["etag"] = f'"v{sku.version}"'
    return _sku_out(sku)


@router.delete("/v1/orgs/{org_id}/products/{product_id}/skus/{sku_id}", response_model=None)
async def delete_sku(
    org_id: UUID,
    product_id: UUID,
    sku_id: UUID,
    principal: Principal = Depends(require_update_product),
    if_match: str = Header(..., alias="If-Match"),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    try:
        await service.archive_sku(
            session,
            org_id=org_id,
            actor_id=_sf_uid(principal),
            product_id=product_id,
            sku_id=sku_id,
            if_match=_if_match_version(if_match),
        )
    except service.VersionConflictError as exc:
        current = await repository.get_sku(session, sku_id, org_id=org_id)
        headers = {"etag": f'"v{current.version}"'} if current else None
        raise HTTPException(
            status_code=412, detail="version conflict, refetch and retry", headers=headers
        ) from exc
    return {"status": "archived"}


# --- promotions ------------------------------------------------------------------


class PromotionResponse(BaseModel):
    id: UUID
    org_id: UUID
    code: str
    kind: str
    value: int
    min_subtotal_minor: int
    starts_at: datetime
    ends_at: datetime
    status: str


def _promotion_out(p: Promotion) -> PromotionResponse:
    return PromotionResponse(
        id=p.id,
        org_id=p.org_id,
        code=p.code,
        kind=p.kind,
        value=p.value,
        min_subtotal_minor=p.min_subtotal_minor,
        starts_at=p.starts_at,
        ends_at=p.ends_at,
        status=p.status,
    )


class CreatePromotionRequest(BaseModel):
    model_config = {"extra": "forbid"}
    code: str
    kind: str
    value: int
    min_subtotal_minor: int = 0
    starts_at: datetime
    ends_at: datetime


@router.get("/v1/orgs/{org_id}/promotions", response_model=list[PromotionResponse])
async def list_promotions(
    org_id: UUID,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_read_promotion),
) -> list[PromotionResponse]:
    return [_promotion_out(p) for p in await repository.list_promotions(session, org_id)]


@router.post("/v1/orgs/{org_id}/promotions", response_model=PromotionResponse)
async def create_promotion(
    org_id: UUID,
    body: CreatePromotionRequest,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_create_promotion),
) -> PromotionResponse:
    try:
        promo = await service.create_promotion(
            session,
            org_id=org_id,
            code=body.code,
            kind=body.kind,
            value=body.value,
            min_subtotal_minor=body.min_subtotal_minor,
            starts_at=body.starts_at,
            ends_at=body.ends_at,
        )
    except service.InvalidPromotionWindow as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _promotion_out(promo)


class UpdatePromotionStatusRequest(BaseModel):
    model_config = {"extra": "forbid"}
    status: str


@router.patch("/v1/orgs/{org_id}/promotions/{promotion_id}", response_model=PromotionResponse)
async def update_promotion(
    org_id: UUID,
    promotion_id: UUID,
    body: UpdatePromotionStatusRequest,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_update_promotion),
) -> PromotionResponse:
    if body.status not in ("active", "disabled"):
        raise HTTPException(status_code=422, detail="status must be 'active' or 'disabled'")
    try:
        promo = await service.set_promotion_status(
            session, promotion_id, org_id=org_id, status=body.status
        )
    except service.NotFoundError as exc:
        raise HTTPException(status_code=404) from exc
    return _promotion_out(promo)


@router.delete("/v1/orgs/{org_id}/promotions/{promotion_id}", response_model=None)
async def delete_promotion(
    org_id: UUID,
    promotion_id: UUID,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_delete_promotion),
) -> dict[str, str]:
    try:
        await service.delete_promotion(session, promotion_id, org_id=org_id)
    except service.NotFoundError as exc:
        raise HTTPException(status_code=404) from exc
    return {"status": "deleted"}


# --- pricing (the pricing authority, LLD-05 §3) -----------------------------------


class QuoteLineIn(BaseModel):
    model_config = {"extra": "forbid"}
    sku_id: UUID
    qty: int = Field(ge=pricing.MIN_QTY, le=pricing.MAX_QTY)


class QuoteRequest(BaseModel):
    model_config = {"extra": "forbid"}
    lines: list[QuoteLineIn] = Field(min_length=1)
    promo_code: str | None = None


class QuoteLineOut(BaseModel):
    sku_id: UUID
    product_id: UUID
    product_name: str
    qty: int
    available: bool
    unit_price_minor: int
    line_total_minor: int


class QuoteResponse(BaseModel):
    valid: bool
    lines: list[QuoteLineOut]
    subtotal_minor: int
    discount_minor: int
    shipping_minor: int
    tax_minor: int
    total_minor: int
    currency: str
    computed_at: datetime
    pricing_fingerprint: str


@router.post("/internal/v1/orgs/{org_id}/quotes", response_model=QuoteResponse)
async def quote_route(
    org_id: UUID,
    body: QuoteRequest,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_quote_scope),
) -> QuoteResponse:
    request = service.QuoteRequest(
        lines=[pricing.QuoteLineRequest(sku_id=line.sku_id, qty=line.qty) for line in body.lines],
        promo_code=body.promo_code,
    )
    try:
        result = await service.quote(session, org_id, request)
    except pricing.StoreUnavailable as exc:
        raise HTTPException(status_code=409, detail="store-unavailable") from exc
    return QuoteResponse(
        valid=result.valid,
        lines=[
            QuoteLineOut(
                sku_id=line.sku_id,
                product_id=line.product_id,
                product_name=line.product_name,
                qty=line.qty,
                available=line.available,
                unit_price_minor=line.unit_price_minor,
                line_total_minor=line.line_total_minor,
            )
            for line in result.lines
        ],
        subtotal_minor=result.subtotal_minor,
        discount_minor=result.discount_minor,
        shipping_minor=result.shipping_minor,
        tax_minor=result.tax_minor,
        total_minor=result.total_minor,
        currency=result.currency,
        computed_at=result.computed_at,
        pricing_fingerprint=result.pricing_fingerprint,
    )


# --- SKU lookup (cart-svc's add-to-cart validation, LLD-06 §2) -------------------


class SkuLookupResponse(BaseModel):
    sku_id: UUID
    product_id: UUID
    product_name: str
    sku_status: str
    product_status: str
    price_minor: int


@router.get("/internal/v1/orgs/{org_id}/skus/{sku_id}", response_model=SkuLookupResponse)
async def get_sku_internal(
    org_id: UUID,
    sku_id: UUID,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_sku_read_scope),
) -> SkuLookupResponse:
    """ "Validates that the SKU belongs to orgId" (LLD-06 §2's PUT cart-item
    rule). Reuses ``quote_inputs``'s one-row-or-none query -- the same
    org-scoped join the quote endpoint already relies on, so a SKU from
    another org 404s exactly like an unknown one would."""
    skus = await repository.quote_inputs(session, org_id, [sku_id])
    sku = skus.get(sku_id)
    if sku is None:
        raise HTTPException(status_code=404)
    return SkuLookupResponse(
        sku_id=sku.sku_id,
        product_id=sku.product_id,
        product_name=sku.product_name,
        sku_status=sku.sku_status,
        product_status=sku.product_status,
        price_minor=sku.price_minor,
    )


# --- export (search-svc's reindex job, LLD-05 §9) ---------------------------------


class ExportPage(BaseModel):
    items: list[dict[str, object]]
    next_cursor: str | None


@router.get("/internal/v1/orgs/{org_id}/products:export", response_model=ExportPage)
async def export_products(
    org_id: UUID,
    cursor: str | None = Query(default=None),
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_export_scope),
) -> ExportPage:
    decoded = _decode_cursor(cursor) if cursor else None
    items, next_cursor = await service.export_products(session, org_id, cursor=decoded, limit=limit)
    return ExportPage(
        items=items,
        next_cursor=_encode_cursor(*next_cursor) if next_cursor else None,
    )
