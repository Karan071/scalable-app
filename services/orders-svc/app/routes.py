"""orders-svc HTTP routes (LLD-06 §3, §6). Every route declares
``Authorize(...)`` (LLD-02 §6); ``org_domain_from_path()`` resolves the
org domain from the URL path, never the body (FR-ORG-05).
"""

import base64
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sf_authz import Authorize, Principal, org_domain_from_path
from sqlalchemy.ext.asyncio import AsyncSession

from . import repository, service
from .db import get_session, get_session_factory
from .models import Order, OrderLine, OrderStatusHistory
from .settings import Settings

router = APIRouter()

_org_domain = org_domain_from_path()

require_checkout = Authorize("order", "create", domain="customer")
require_read_own_order = Authorize("order", "read", domain="customer")
require_cancel_own_order = Authorize("order", "cancel", domain="customer")
require_read_org_orders = Authorize("order", "read", domain=_org_domain)
require_update_order_status = Authorize("order", "update_status", domain=_org_domain)
require_cancel_org_order = Authorize("order", "cancel", domain=_org_domain)
require_refund = Authorize("refund", "create", domain=_org_domain)

_DEFAULT_LIMIT = 24
_MAX_LIMIT = 100


def _sf_uid(principal: Principal) -> UUID:
    raw = principal.claim("sf_uid")
    if raw is None:
        raise HTTPException(
            status_code=403, detail="token has no sf_uid claim yet; refresh required"
        )
    return UUID(str(raw))


def _decode_cursor(cursor: str) -> UUID:
    try:
        return UUID(base64.urlsafe_b64decode(cursor + "==").decode())
    except Exception as exc:
        raise HTTPException(status_code=422, detail="invalid cursor") from exc


def _encode_cursor(value: UUID) -> str:
    return base64.urlsafe_b64encode(str(value).encode()).rstrip(b"=").decode()


def _if_match_version(if_match: str) -> int:
    try:
        return int(if_match.strip('"').removeprefix("v"))
    except ValueError as exc:
        raise HTTPException(status_code=428, detail="invalid If-Match") from exc


def _result_to_response(result: service.ActionResult) -> dict:
    if result.status_code >= 300:
        raise HTTPException(
            status_code=result.status_code, detail=result.body.get("title", "error")
        )
    return result.body


def _participants(request: Request) -> service.Participants:
    return request.app.state.participants


# --- checkout (LLD-06 §3) ---------------------------------------------------


class ShippingAddress(BaseModel):
    model_config = {"extra": "forbid"}
    name: str
    line1: str
    line2: str | None = None
    city: str
    postal_code: str
    country: str = Field(min_length=2, max_length=2)


class CheckoutRequest(BaseModel):
    model_config = {"extra": "forbid"}
    cart_version: int = Field(ge=1)
    promo_code: str | None = None
    shipping_address: ShippingAddress
    payment_method_token: str = Field(min_length=1, max_length=200)


@router.post("/v1/stores/{org_id}/checkouts", status_code=202)
async def checkout(
    org_id: UUID,
    body: CheckoutRequest,
    request: Request,
    idempotency_key: UUID = Header(..., alias="Idempotency-Key"),
    principal: Principal = Depends(require_checkout),
) -> dict:
    settings: Settings = request.app.state.settings
    try:
        result = await service.checkout(
            get_session_factory(request),
            _participants(request),
            customer_id=_sf_uid(principal),
            org_id=org_id,
            idempotency_key=idempotency_key,
            cart_version=body.cart_version,
            promo_code=body.promo_code,
            shipping_address=body.shipping_address.model_dump(),
            payment_method_token=body.payment_method_token,
            reservation_ttl_seconds=settings.reservation_ttl_seconds,
            saga_deadline_seconds=settings.saga_deadline_seconds,
        )
    except service.CheckoutInProgress as exc:
        raise HTTPException(
            status_code=409, detail="checkout-in-progress", headers={"retry-after": "2"}
        ) from exc
    except service.IdempotencyKeyReused as exc:
        raise HTTPException(status_code=422, detail="idempotency-key-reused") from exc
    body_out = _result_to_response(result)
    return body_out


# --- customer order views (LLD-06 §6) ---------------------------------------


class OrderLineOut(BaseModel):
    sku_id: UUID
    product_id: UUID
    name_snapshot: str
    unit_price_minor: int
    qty: int
    line_total_minor: int


class OrderStatusHistoryOut(BaseModel):
    from_status: str | None
    to_status: str
    actor_type: str
    reason: str | None
    at: datetime


class OrderOut(BaseModel):
    id: UUID
    org_id: UUID
    status: str
    payment_status: str
    currency: str
    subtotal_minor: int
    discount_minor: int
    shipping_minor: int
    tax_minor: int
    total_minor: int
    promo_code: str | None
    cancel_reason: str | None
    version: int
    created_at: datetime
    updated_at: datetime


class OrderDetailOut(OrderOut):
    lines: list[OrderLineOut]
    history: list[OrderStatusHistoryOut]


def _order_out(order: Order) -> OrderOut:
    return OrderOut(
        id=order.id,
        org_id=order.org_id,
        status=order.status,
        payment_status=order.payment_status,
        currency=order.currency,
        subtotal_minor=order.subtotal_minor,
        discount_minor=order.discount_minor,
        shipping_minor=order.shipping_minor,
        tax_minor=order.tax_minor,
        total_minor=order.total_minor,
        promo_code=order.promo_code,
        cancel_reason=order.cancel_reason,
        version=order.version,
        created_at=order.created_at,
        updated_at=order.updated_at,
    )


def _line_out(line: OrderLine) -> OrderLineOut:
    return OrderLineOut(
        sku_id=line.sku_id,
        product_id=line.product_id,
        name_snapshot=line.name_snapshot,
        unit_price_minor=line.unit_price_minor,
        qty=line.qty,
        line_total_minor=line.line_total_minor,
    )


def _history_out(row: OrderStatusHistory) -> OrderStatusHistoryOut:
    return OrderStatusHistoryOut(
        from_status=row.from_status,
        to_status=row.to_status,
        actor_type=row.actor_type,
        reason=row.reason,
        at=row.at,
    )


class OrderListResponse(BaseModel):
    items: list[OrderOut]
    next_cursor: str | None


@router.get("/v1/me/orders", response_model=OrderListResponse)
async def list_my_orders(
    store: UUID | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=_DEFAULT_LIMIT, le=_MAX_LIMIT),
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_read_own_order),
) -> OrderListResponse:
    decoded_cursor = _decode_cursor(cursor) if cursor else None
    orders = await repository.list_orders_for_customer(
        session, customer_id=_sf_uid(principal), org_id=store, cursor=decoded_cursor, limit=limit
    )
    next_cursor = _encode_cursor(orders[-1].id) if len(orders) == limit else None
    return OrderListResponse(items=[_order_out(o) for o in orders], next_cursor=next_cursor)


@router.get("/v1/me/orders/{order_id}", response_model=OrderDetailOut)
async def get_my_order(
    order_id: UUID,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_read_own_order),
) -> OrderDetailOut:
    order = await repository.get_order_for_customer(
        session, order_id, customer_id=_sf_uid(principal)
    )
    if order is None:
        raise HTTPException(status_code=404)
    lines = await repository.list_order_lines(session, order_id)
    history = await repository.list_order_status_history(session, order_id)
    return OrderDetailOut(
        **_order_out(order).model_dump(),
        lines=[_line_out(line) for line in lines],
        history=[_history_out(row) for row in history],
    )


class CancelResponse(BaseModel):
    order_id: UUID
    status: str


@router.post("/v1/me/orders/{order_id}:cancel", response_model=CancelResponse)
async def cancel_my_order(
    order_id: UUID,
    request: Request,
    idempotency_key: UUID = Header(..., alias="Idempotency-Key"),
    principal: Principal = Depends(require_cancel_own_order),
) -> CancelResponse:
    try:
        result = await service.customer_cancel_order(
            get_session_factory(request),
            order_id=order_id,
            customer_id=_sf_uid(principal),
            idempotency_key=idempotency_key,
        )
    except service.CheckoutInProgress as exc:
        raise HTTPException(
            status_code=409, detail="checkout-in-progress", headers={"retry-after": "2"}
        ) from exc
    except service.IdempotencyKeyReused as exc:
        raise HTTPException(status_code=422, detail="idempotency-key-reused") from exc
    body = _result_to_response(result)
    return CancelResponse(order_id=order_id, status=body["status"])


class OrderStatusOut(BaseModel):
    order_id: UUID
    status: str
    payment_status: str


@router.get("/v1/orders/{order_id}/status", response_model=OrderStatusOut)
async def order_status(
    order_id: UUID,
    wait: int = Query(default=0, ge=0, le=25),
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_read_own_order),
) -> OrderStatusOut:
    """LLD-06 §10's long-poll fallback. ``wait`` is accepted but not
    honored yet -- this returns the current snapshot immediately. Actually
    blocking until a change (or ``wait`` elapses) needs the same
    redis-cache fan-out SSE uses (LLD-06 §10), which is E4.7 (M4) scope;
    deferred alongside it rather than half-building a second mechanism."""
    order = await repository.get_order_for_customer(
        session, order_id, customer_id=_sf_uid(principal)
    )
    if order is None:
        raise HTTPException(status_code=404)
    return OrderStatusOut(
        order_id=order.id, status=order.status, payment_status=order.payment_status
    )


# --- admin order views (LLD-06 §6) ------------------------------------------


@router.get("/v1/orgs/{org_id}/orders", response_model=OrderListResponse)
async def list_org_orders(
    org_id: UUID,
    status: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=_DEFAULT_LIMIT, le=_MAX_LIMIT),
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_read_org_orders),
) -> OrderListResponse:
    decoded_cursor = _decode_cursor(cursor) if cursor else None
    orders = await repository.list_orders_for_org(
        session, org_id=org_id, status=status, cursor=decoded_cursor, limit=limit
    )
    next_cursor = _encode_cursor(orders[-1].id) if len(orders) == limit else None
    return OrderListResponse(items=[_order_out(o) for o in orders], next_cursor=next_cursor)


@router.get("/v1/orgs/{org_id}/orders/{order_id}", response_model=OrderDetailOut)
async def get_org_order(
    org_id: UUID,
    order_id: UUID,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_read_org_orders),
) -> OrderDetailOut:
    order = await repository.get_order_for_org(session, order_id, org_id=org_id)
    if order is None:
        raise HTTPException(status_code=404)
    lines = await repository.list_order_lines(session, order_id)
    history = await repository.list_order_status_history(session, order_id)
    return OrderDetailOut(
        **_order_out(order).model_dump(),
        lines=[_line_out(line) for line in lines],
        history=[_history_out(row) for row in history],
    )


class TransitionRequest(BaseModel):
    model_config = {"extra": "forbid"}
    to: str
    tracking_ref: str | None = None


@router.post("/v1/orgs/{org_id}/orders/{order_id}:transition", response_model=OrderOut)
async def transition_org_order(
    org_id: UUID,
    order_id: UUID,
    body: TransitionRequest,
    if_match: str = Header(..., alias="If-Match"),
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_update_order_status),
) -> OrderOut:
    try:
        order = await service.admin_transition_order(
            session,
            order_id,
            org_id=org_id,
            actor_id=_sf_uid(principal),
            to_status=body.to,
            tracking_ref=body.tracking_ref,
            expected_version=_if_match_version(if_match),
        )
    except service.OrderNotFound as exc:
        raise HTTPException(status_code=404) from exc
    except service.VersionConflict as exc:
        raise HTTPException(status_code=412, detail="version conflict, refetch and retry") from exc
    except service.InvalidTransition as exc:
        raise HTTPException(status_code=409, detail="invalid-transition") from exc
    return _order_out(order)


@router.post("/v1/orgs/{org_id}/orders/{order_id}:cancel", response_model=CancelResponse)
async def cancel_org_order(
    org_id: UUID,
    order_id: UUID,
    request: Request,
    idempotency_key: UUID = Header(..., alias="Idempotency-Key"),
    if_match: str = Header(..., alias="If-Match"),
    principal: Principal = Depends(require_cancel_org_order),
) -> CancelResponse:
    try:
        result = await service.admin_cancel_order(
            get_session_factory(request),
            order_id=order_id,
            org_id=org_id,
            actor_id=_sf_uid(principal),
            idempotency_key=idempotency_key,
            expected_version=_if_match_version(if_match),
        )
    except service.CheckoutInProgress as exc:
        raise HTTPException(
            status_code=409, detail="checkout-in-progress", headers={"retry-after": "2"}
        ) from exc
    except service.IdempotencyKeyReused as exc:
        raise HTTPException(status_code=422, detail="idempotency-key-reused") from exc
    body = _result_to_response(result)
    return CancelResponse(order_id=order_id, status=body["status"])


class RefundResponse(BaseModel):
    order_id: UUID
    payment_status: str


@router.post(
    "/v1/orgs/{org_id}/orders/{order_id}:refund", status_code=202, response_model=RefundResponse
)
async def refund_org_order(
    org_id: UUID,
    order_id: UUID,
    request: Request,
    idempotency_key: UUID = Header(..., alias="Idempotency-Key"),
    principal: Principal = Depends(require_refund),
) -> RefundResponse:
    try:
        result = await service.admin_refund_order(
            get_session_factory(request),
            order_id=order_id,
            org_id=org_id,
            actor_id=_sf_uid(principal),
            idempotency_key=idempotency_key,
        )
    except service.CheckoutInProgress as exc:
        raise HTTPException(
            status_code=409, detail="checkout-in-progress", headers={"retry-after": "2"}
        ) from exc
    except service.IdempotencyKeyReused as exc:
        raise HTTPException(status_code=422, detail="idempotency-key-reused") from exc
    body = _result_to_response(result)
    return RefundResponse(order_id=order_id, payment_status=body["payment_status"])
