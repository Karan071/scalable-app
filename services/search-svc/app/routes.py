"""search-svc HTTP routes (LLD-05 §8). Unauthenticated browse: every route
is ``Public`` -- there's no session to check and no tenant secret here,
only an org's already-public catalog (LLD-02 §6).
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sf_authz import Public
from sqlalchemy.ext.asyncio import AsyncSession

from . import service
from .db import get_session

router = APIRouter()

require_public = Public(rate_class="search")


class SearchHitOut(BaseModel):
    product_id: UUID
    slug: str
    name: str
    primary_image_key: str | None
    min_price_minor: int | None
    max_price_minor: int | None
    in_stock: bool


class SearchResponse(BaseModel):
    items: list[SearchHitOut]
    next_cursor: str | None


@router.get("/v1/stores/{org_id}/search", response_model=SearchResponse)
async def search(
    org_id: UUID,
    request: Request,
    q: str | None = Query(default=None),
    category: UUID | None = Query(default=None),
    price_min: int | None = Query(default=None),
    price_max: int | None = Query(default=None),
    in_stock: bool | None = Query(default=None),
    sort: str | None = Query(default=None),
    limit: int = Query(default=24),
    cursor: str | None = Query(default=None),
    page: int | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
    _: None = Depends(require_public),
) -> SearchResponse:
    raw = service.RawSearchRequest(
        q=q,
        category=category,
        price_min=price_min,
        price_max=price_max,
        in_stock=in_stock,
        sort=sort,
        limit=limit,
        cursor=cursor,
        page=page,
    )
    try:
        result = await service.run_search(
            session, org_id, raw, secret_b64=request.app.state.settings.cursor_secret_b64
        )
    except service.SearchValidationError as exc:
        # LLD-05 §13: query validation failures are 400, not the usual 422.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return SearchResponse(
        items=[
            SearchHitOut(
                product_id=h.product_id,
                slug=h.slug,
                name=h.name,
                primary_image_key=h.primary_image_key,
                min_price_minor=h.min_price_minor,
                max_price_minor=h.max_price_minor,
                in_stock=h.in_stock,
            )
            for h in result.hits
        ],
        next_cursor=result.next_cursor,
    )
