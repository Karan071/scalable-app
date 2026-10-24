"""cart-svc HTTP routes (LLD-06 §2). Every customer-facing route declares
``Authorize(..., domain="customer")`` -- a real Casbin check, not just
``AuthenticatedUser()``: a token without ``role:customer`` (shouldn't
happen; it's granted on signup, LLD-02 §9) is refused rather than silently
treated as this customer's own cart. Ownership itself (this customer's own
cart, never another's) is enforced in ``repository.py``'s own
``customer_id`` filters, not by Casbin -- there is no Casbin resource for
"my cart" vs. "someone else's cart".
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sf_authz import Authorize, Internal, Principal
from sqlalchemy.ext.asyncio import AsyncSession

from . import repository, service
from .db import get_session
from .models import Cart, CartItem

router = APIRouter()

require_read_cart = Authorize("cart", "read", domain="customer")
require_update_cart = Authorize("cart", "update", domain="customer")
require_delete_cart = Authorize("cart", "delete", domain="customer")
require_internal_cart_read = Internal("cart:read")


def _sf_uid(principal: Principal) -> UUID:
    raw = principal.claim("sf_uid")
    if raw is None:
        raise HTTPException(
            status_code=403, detail="token has no sf_uid claim yet; refresh required"
        )
    return UUID(str(raw))


class CartItemOut(BaseModel):
    sku_id: UUID
    product_id: UUID
    qty: int


class CartResponse(BaseModel):
    id: UUID
    org_id: UUID
    status: str
    version: int
    items: list[CartItemOut]


def _item_out(item: CartItem) -> CartItemOut:
    return CartItemOut(sku_id=item.sku_id, product_id=item.product_id, qty=item.qty)


def _cart_out(cart: Cart, items: list[CartItem]) -> CartResponse:
    return CartResponse(
        id=cart.id,
        org_id=cart.org_id,
        status=cart.status,
        version=cart.version,
        items=[_item_out(i) for i in items],
    )


@router.get("/v1/me/carts/{org_id}", response_model=CartResponse)
async def get_cart(
    org_id: UUID,
    principal: Principal = Depends(require_read_cart),
    session: AsyncSession = Depends(get_session),
) -> CartResponse:
    view = await service.get_cart(session, customer_id=_sf_uid(principal), org_id=org_id)
    return _cart_out(view.cart, view.items)


class SetItemRequest(BaseModel):
    model_config = {"extra": "forbid"}
    qty: int = Field(ge=1, le=99)


@router.put("/v1/me/carts/{org_id}/items/{sku_id}", response_model=CartResponse)
async def set_item(
    org_id: UUID,
    sku_id: UUID,
    body: SetItemRequest,
    request: Request,
    principal: Principal = Depends(require_update_cart),
    session: AsyncSession = Depends(get_session),
) -> CartResponse:
    try:
        view = await service.set_item(
            session,
            customer_id=_sf_uid(principal),
            org_id=org_id,
            sku_id=sku_id,
            qty=body.qty,
            catalog=request.app.state.catalog_client,
        )
    except service.SkuNotFound as exc:
        raise HTTPException(status_code=404, detail="sku not found in this store") from exc
    except service.SkuUnavailable as exc:
        raise HTTPException(status_code=409, detail="sku-unavailable") from exc
    except service.CartFull as exc:
        raise HTTPException(status_code=409, detail="cart-full") from exc
    return _cart_out(view.cart, view.items)


@router.delete("/v1/me/carts/{org_id}/items/{sku_id}", response_model=CartResponse)
async def delete_item(
    org_id: UUID,
    sku_id: UUID,
    principal: Principal = Depends(require_delete_cart),
    session: AsyncSession = Depends(get_session),
) -> CartResponse:
    customer_id = _sf_uid(principal)
    view = await service.remove_item(session, customer_id=customer_id, org_id=org_id, sku_id=sku_id)
    if view is None:
        # Idempotent (LLD-06 §2): no active cart at all is still success,
        # reported as an empty one rather than manufacturing a 404.
        view = await service.get_cart(session, customer_id=customer_id, org_id=org_id)
    return _cart_out(view.cart, view.items)


@router.delete("/v1/me/carts/{org_id}/items", response_model=CartResponse)
async def clear_items(
    org_id: UUID,
    principal: Principal = Depends(require_delete_cart),
    session: AsyncSession = Depends(get_session),
) -> CartResponse:
    customer_id = _sf_uid(principal)
    view = await service.clear_cart(session, customer_id=customer_id, org_id=org_id)
    if view is None:
        view = await service.get_cart(session, customer_id=customer_id, org_id=org_id)
    return _cart_out(view.cart, view.items)


class CartSnapshotResponse(BaseModel):
    id: UUID
    customer_id: UUID
    org_id: UUID
    version: int
    items: list[CartItemOut]


@router.get("/internal/v1/carts/{cart_id}:snapshot", response_model=CartSnapshotResponse)
async def cart_snapshot(
    cart_id: UUID,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_internal_cart_read),
) -> CartSnapshotResponse:
    """For orders-svc's checkout (LLD-06 §3): items + version, read-only,
    no ownership check -- the internal caller is trusted (ADR-0009 §2)."""
    cart = await session.get(Cart, cart_id)
    if cart is None:
        raise HTTPException(status_code=404)
    items = await repository.list_items(session, cart_id)
    return CartSnapshotResponse(
        id=cart.id,
        customer_id=cart.customer_id,
        org_id=cart.org_id,
        version=cart.version,
        items=[_item_out(i) for i in items],
    )
