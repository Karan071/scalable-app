"""inventory-svc HTTP routes (LLD-07 §8). Every route declares `Authorize(...)`,
`Internal(...)`, or `Public(...)` (LLD-02 §6).
"""

import base64
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sf_authz import Authorize, Internal, Principal, Public, org_domain_from_path
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from . import repository, service
from .db import get_session, get_session_factory
from .models import Reservation, StockItem
from .repository import MAX_AVAILABILITY_SKUS
from .settings import Settings

router = APIRouter()

_org_domain = org_domain_from_path()

require_reservations_write = Internal("inventory:reservations:write")
require_reservations_read = Internal("inventory:reservations:read")
require_availability_public = Public(rate_class="availability")
require_stock_read = Authorize("stock", "read", domain=_org_domain)
require_stock_adjust = Authorize("stock", "adjust", domain=_org_domain)
require_reservation_read = Authorize("reservation", "read", domain=_org_domain)
require_reservation_release = Authorize("reservation", "release", domain=_org_domain)

_DEFAULT_LIMIT = 50
_MAX_LIMIT = 200


def _sf_uid(principal: Principal) -> UUID:
    raw = principal.claim("sf_uid")
    if raw is None:
        raise HTTPException(
            status_code=403, detail="token has no sf_uid claim yet; refresh required"
        )
    return UUID(str(raw))


def _decode_uuid_cursor(cursor: str) -> UUID:
    try:
        return UUID(base64.urlsafe_b64decode(cursor + "==").decode())
    except Exception as exc:
        raise HTTPException(status_code=422, detail="invalid cursor") from exc


def _encode_uuid_cursor(value: UUID) -> str:
    return base64.urlsafe_b64encode(str(value).encode()).rstrip(b"=").decode()


def _if_match_version(if_match: str) -> int:
    try:
        return int(if_match.strip('"').removeprefix("v"))
    except ValueError as exc:
        raise HTTPException(status_code=428, detail="invalid If-Match") from exc


# --- reserve/commit/release (internal, LLD-07 §4.1-§4.3) --------------------


class ReserveLine(BaseModel):
    model_config = {"extra": "forbid"}
    sku_id: UUID
    qty: int = Field(gt=0)


class ReserveRequest(BaseModel):
    model_config = {"extra": "forbid"}
    order_id: UUID
    org_id: UUID
    lines: list[ReserveLine] = Field(min_length=1)
    ttl_seconds: int | None = Field(default=None, ge=300, le=3600)
    on_behalf_of: UUID | None = None


class ReserveResponse(BaseModel):
    reservation_id: UUID
    expires_at: datetime


@router.post("/internal/v1/reservations", status_code=201, response_model=ReserveResponse)
async def reserve(
    body: ReserveRequest,
    request: Request,
    _: Principal = Depends(require_reservations_write),
) -> ReserveResponse:
    settings: Settings = request.app.state.settings
    session_factory: async_sessionmaker[AsyncSession] = get_session_factory(request)
    outcome = await service.reserve(
        session_factory,
        org_id=body.org_id,
        order_id=body.order_id,
        lines=[(line.sku_id, line.qty) for line in body.lines],
        ttl_seconds=body.ttl_seconds or settings.reservation_ttl_seconds,
        on_behalf_of=body.on_behalf_of,
    )
    if outcome.shortfalls is not None:
        raise HTTPException(status_code=409, detail="insufficient-stock")
    return ReserveResponse(
        reservation_id=outcome.reservation.id, expires_at=outcome.reservation.expires_at
    )


class CommitResponse(BaseModel):
    reservation_id: UUID
    status: str


@router.post("/internal/v1/reservations/{order_id}:commit", response_model=CommitResponse)
async def commit_reservation(
    order_id: UUID,
    request: Request,
    _: Principal = Depends(require_reservations_write),
) -> CommitResponse:
    session_factory = get_session_factory(request)
    try:
        reservation = await service.commit(session_factory, order_id=order_id)
    except service.ReservationNotFound as exc:
        raise HTTPException(status_code=404, detail="reservation not found") from exc
    except service.ReservationNotActive as exc:
        raise HTTPException(status_code=409, detail="reservation-not-active") from exc
    return CommitResponse(reservation_id=reservation.id, status=reservation.status)


class ReleaseRequest(BaseModel):
    model_config = {"extra": "forbid"}
    org_id: UUID
    reason: str | None = None


@router.post("/internal/v1/reservations/{order_id}:release", response_model=CommitResponse)
async def release_reservation(
    order_id: UUID,
    body: ReleaseRequest,
    request: Request,
    _: Principal = Depends(require_reservations_write),
) -> CommitResponse:
    session_factory = get_session_factory(request)
    try:
        reservation = await service.release(
            session_factory, org_id=body.org_id, order_id=order_id, reason=body.reason
        )
    except service.ReservationCommitted as exc:
        raise HTTPException(status_code=409, detail="reservation-committed") from exc
    return CommitResponse(reservation_id=reservation.id, status=reservation.status)


class ReservationLineOut(BaseModel):
    sku_id: UUID
    qty: int


class ReservationDetail(BaseModel):
    id: UUID
    org_id: UUID
    order_id: UUID
    status: str
    expires_at: datetime
    reason: str | None
    lines: list[ReservationLineOut]


def _reservation_out(
    reservation: Reservation, lines: list[ReservationLineOut]
) -> ReservationDetail:
    return ReservationDetail(
        id=reservation.id,
        org_id=reservation.org_id,
        order_id=reservation.order_id,
        status=reservation.status,
        expires_at=reservation.expires_at,
        reason=reservation.reason,
        lines=lines,
    )


@router.get("/internal/v1/reservations/{order_id}", response_model=ReservationDetail)
async def get_reservation_by_order(
    order_id: UUID,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_reservations_read),
) -> ReservationDetail:
    reservation = await repository.get_reservation_by_order(session, order_id)
    if reservation is None:
        raise HTTPException(status_code=404)
    lines = await repository.list_reservation_lines(session, reservation.id)
    return _reservation_out(
        reservation, [ReservationLineOut(sku_id=line.sku_id, qty=line.qty) for line in lines]
    )


class ReservationListResponse(BaseModel):
    items: list[ReservationDetail]
    next_cursor: str | None


@router.get("/internal/v1/reservations", response_model=ReservationListResponse)
async def list_reservations_internal(
    org_id: UUID,
    status: str | None = Query(default=None),
    older_than: datetime | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=_DEFAULT_LIMIT, le=_MAX_LIMIT),
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_reservations_read),
) -> ReservationListResponse:
    decoded_cursor = _decode_uuid_cursor(cursor) if cursor else None
    reservations = await repository.list_reservations(
        session,
        org_id=org_id,
        status=status,
        older_than=older_than,
        cursor=decoded_cursor,
        limit=limit,
    )
    items = []
    for reservation in reservations:
        lines = await repository.list_reservation_lines(session, reservation.id)
        items.append(
            _reservation_out(
                reservation,
                [ReservationLineOut(sku_id=line.sku_id, qty=line.qty) for line in lines],
            )
        )
    next_cursor = _encode_uuid_cursor(reservations[-1].id) if len(reservations) == limit else None
    return ReservationListResponse(items=items, next_cursor=next_cursor)


# --- availability (public, LLD-07 §7-§8) ------------------------------------


class AvailabilityItem(BaseModel):
    sku_id: UUID
    in_stock: bool
    low_stock: bool


class AvailabilityResponse(BaseModel):
    items: list[AvailabilityItem]


@router.get("/v1/stores/{org_id}/availability", response_model=AvailabilityResponse)
async def get_availability(
    org_id: UUID,
    sku_ids: list[UUID] = Query(...),
    session: AsyncSession = Depends(get_session),
    _: None = Depends(require_availability_public),
) -> AvailabilityResponse:
    if len(sku_ids) > MAX_AVAILABILITY_SKUS:
        raise HTTPException(status_code=422, detail="too many sku_ids")
    items = await repository.get_stock_items(session, org_id=org_id, sku_ids=sku_ids)
    out = []
    for sku_id in sku_ids:
        item = items.get(sku_id)
        if item is None:
            out.append(AvailabilityItem(sku_id=sku_id, in_stock=False, low_stock=False))
            continue
        available = item.on_hand - item.reserved
        out.append(
            AvailabilityItem(
                sku_id=sku_id,
                in_stock=available > 0,
                low_stock=available <= item.low_stock_threshold,
            )
        )
    return AvailabilityResponse(items=out)


# --- admin: stock (LLD-07 §4.5, §8) -----------------------------------------


class StockItemOut(BaseModel):
    sku_id: UUID
    product_id: UUID
    on_hand: int
    reserved: int
    available: int
    low_stock_threshold: int
    version: int


def _stock_item_out(item: StockItem) -> StockItemOut:
    return StockItemOut(
        sku_id=item.sku_id,
        product_id=item.product_id,
        on_hand=item.on_hand,
        reserved=item.reserved,
        available=item.on_hand - item.reserved,
        low_stock_threshold=item.low_stock_threshold,
        version=item.version,
    )


class StockListResponse(BaseModel):
    items: list[StockItemOut]
    next_cursor: str | None


@router.get("/v1/orgs/{org_id}/stock", response_model=StockListResponse)
async def list_stock(
    org_id: UUID,
    low_stock: bool | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=_DEFAULT_LIMIT, le=_MAX_LIMIT),
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_stock_read),
) -> StockListResponse:
    decoded_cursor = _decode_uuid_cursor(cursor) if cursor else None
    items = await repository.list_stock_items(
        session, org_id=org_id, low_stock=low_stock, cursor=decoded_cursor, limit=limit
    )
    next_cursor = _encode_uuid_cursor(items[-1].sku_id) if len(items) == limit else None
    return StockListResponse(items=[_stock_item_out(i) for i in items], next_cursor=next_cursor)


class AdjustStockRequest(BaseModel):
    model_config = {"extra": "forbid"}
    adjustment_id: UUID
    delta: int | None = None
    set_on_hand: int | None = Field(default=None, ge=0)
    reason: str = Field(min_length=1, max_length=500)


@router.post("/v1/orgs/{org_id}/stock/{sku_id}:adjust", response_model=StockItemOut)
async def adjust_stock(
    org_id: UUID,
    sku_id: UUID,
    body: AdjustStockRequest,
    if_match: str = Header(..., alias="If-Match"),
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_stock_adjust),
) -> StockItemOut:
    if (body.delta is None) == (body.set_on_hand is None):
        raise HTTPException(status_code=422, detail="exactly one of delta or set_on_hand required")
    try:
        item = await service.adjust_stock(
            session,
            org_id=org_id,
            sku_id=sku_id,
            actor_id=_sf_uid(principal),
            adjustment_id=body.adjustment_id,
            delta=body.delta,
            set_on_hand=body.set_on_hand,
            reason=body.reason,
            expected_version=_if_match_version(if_match),
        )
    except service.StockItemNotFound as exc:
        raise HTTPException(status_code=404, detail="sku not found in this store") from exc
    except service.BelowReserved as exc:
        raise HTTPException(status_code=409, detail="below-reserved") from exc
    except service.VersionConflict as exc:
        current = await repository.get_stock_item(session, org_id=org_id, sku_id=sku_id)
        headers = {"etag": f'"v{current.version}"'} if current else None
        raise HTTPException(
            status_code=412, detail="version conflict, refetch and retry", headers=headers
        ) from exc
    return _stock_item_out(item)


# --- admin: reservations (LLD-07 §8) ----------------------------------------


@router.get("/v1/orgs/{org_id}/reservations", response_model=ReservationListResponse)
async def list_reservations_admin(
    org_id: UUID,
    status: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=_DEFAULT_LIMIT, le=_MAX_LIMIT),
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_reservation_read),
) -> ReservationListResponse:
    decoded_cursor = _decode_uuid_cursor(cursor) if cursor else None
    reservations = await repository.list_reservations(
        session, org_id=org_id, status=status, older_than=None, cursor=decoded_cursor, limit=limit
    )
    items = []
    for reservation in reservations:
        lines = await repository.list_reservation_lines(session, reservation.id)
        items.append(
            _reservation_out(
                reservation,
                [ReservationLineOut(sku_id=line.sku_id, qty=line.qty) for line in lines],
            )
        )
    next_cursor = _encode_uuid_cursor(reservations[-1].id) if len(reservations) == limit else None
    return ReservationListResponse(items=items, next_cursor=next_cursor)


class ManualReleaseRequest(BaseModel):
    model_config = {"extra": "forbid"}
    reason: str = Field(min_length=1, max_length=500)
    force: bool = False


@router.post(
    "/v1/orgs/{org_id}/reservations/{reservation_id}:release", response_model=CommitResponse
)
async def manual_release_reservation(
    org_id: UUID,
    reservation_id: UUID,
    body: ManualReleaseRequest,
    request: Request,
    _: Principal = Depends(require_reservation_release),
) -> CommitResponse:
    session_factory = get_session_factory(request)
    try:
        reservation = await service.manual_release(
            session_factory,
            org_id=org_id,
            reservation_id=reservation_id,
            reason=body.reason,
            force=body.force,
        )
    except service.ReservationNotFound as exc:
        raise HTTPException(status_code=404, detail="reservation not found") from exc
    except service.ReservationNotActive as exc:
        raise HTTPException(status_code=409, detail="reservation-not-active") from exc
    except service.WithinDeadline as exc:
        raise HTTPException(status_code=409, detail="within-deadline") from exc
    return CommitResponse(reservation_id=reservation.id, status=reservation.status)
