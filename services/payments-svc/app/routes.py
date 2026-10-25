"""payments-svc HTTP routes (LLD-06 §7.1's internal API, §8.2's webhook).

The webhook route carries ``Public(...)`` for ``assert_routes_covered``'s
bookkeeping -- accurate (no Keycloak JWT reaches it), but the real guard
is the HMAC signature check in the handler body, not the marker.
"""

import hashlib
import json
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sf_authz import Internal, Principal, Public
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from . import service
from .db import get_session_factory
from .provider_client import MockProviderClient
from .settings import Settings

router = APIRouter()

require_authorize_scope = Internal("payments:authorize")
require_webhook = Public(rate_class="webhook")


def _provider(request: Request) -> MockProviderClient:
    return request.app.state.provider


# --- internal API (LLD-06 §7.1) ---------------------------------------------


class AuthorizeRequest(BaseModel):
    model_config = {"extra": "forbid"}
    order_id: UUID
    org_id: UUID
    customer_id: UUID
    amount_minor: int = Field(ge=1)
    currency: str = Field(min_length=3, max_length=3)
    payment_method_token: str = Field(min_length=1, max_length=200)


class AuthorizeResponse(BaseModel):
    payment_id: UUID
    status: str


@router.post("/internal/v1/payments", response_model=None)
async def authorize(
    body: AuthorizeRequest,
    request: Request,
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
    provider: MockProviderClient = Depends(_provider),
    _: Principal = Depends(require_authorize_scope),
) -> AuthorizeResponse | JSONResponse:
    outcome = await service.authorize(
        session_factory,
        provider,
        order_id=body.order_id,
        org_id=body.org_id,
        customer_id=body.customer_id,
        amount_minor=body.amount_minor,
        currency=body.currency,
        payment_method_token=body.payment_method_token,
    )
    response = AuthorizeResponse(payment_id=outcome.payment_id, status=outcome.status)
    if outcome.status_code >= 300:
        raise HTTPException(status_code=outcome.status_code, detail="payment-declined")
    if outcome.status_code != 200:
        # FastAPI defaults a route with no `status_code=` to 200; the 202
        # ("unknown", still resolving) case needs a custom JSONResponse
        # since there's no decorator-level override for a dynamic code.
        return JSONResponse(
            status_code=outcome.status_code, content=response.model_dump(mode="json")
        )
    return response


# --- webhook (LLD-06 §8.2) ---------------------------------------------------


@router.post("/webhooks/payments/mock", status_code=200)
async def payments_webhook(
    request: Request,
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
    _: None = Depends(require_webhook),
) -> dict[str, str]:
    settings: Settings = request.app.state.settings
    body = await request.body()
    if len(body) > settings.webhook_max_body_bytes:
        raise HTTPException(status_code=400, detail="payload too large")
    signature_header = request.headers.get("sf-signature")
    if signature_header is None:
        raise HTTPException(status_code=400, detail="missing SF-Signature")

    try:
        service.verify_signature(
            secrets=settings.webhook_secret_list(),
            signature_header=signature_header,
            body=body,
            clock_skew_tolerance_s=settings.webhook_clock_skew_tolerance_seconds,
        )
    except (service.SignatureInvalid, service.TimestampStale) as exc:
        raise HTTPException(status_code=400, detail="invalid signature") from exc

    try:
        event = json.loads(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid JSON") from exc

    result = await service.process_webhook(
        session_factory, event=event, payload_sha256=hashlib.sha256(body).hexdigest()
    )
    return {"result": result}
