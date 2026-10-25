"""mock-payment-provider's public API (ADR-0008): modelled on a common PSP
shape. Outside the trust boundary (LLD-02 §6 doesn't apply) -- no
``Authorize``/``Internal`` here, just the idempotency-key contract every
POST on a real PSP has.
"""

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from app import store as store_mod
from app.store import AuthorizationExpired, CaptureFailed, CardDeclined, IntentNotFound, Store

router = APIRouter()


def _store(request: Request) -> Store:
    return request.app.state.store


def _delay_scale(request: Request) -> float:
    return request.app.state.settings.delay_scale


def _replay_or_run(store: Store, idempotency_key: str, run) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    cached = store.idempotency.get(idempotency_key)
    if cached is not None:
        status_code, body = cached
        if status_code >= 400:
            raise HTTPException(status_code=status_code, detail=body)
        return body
    try:
        status_code, body = run()
    except CardDeclined as exc:
        body = {"error": {"code": "card_declined"}}
        store.idempotency[idempotency_key] = (402, body)
        raise HTTPException(status_code=402, detail=body) from exc
    except IntentNotFound as exc:
        raise HTTPException(
            status_code=404, detail={"error": {"code": "no_such_payment_intent"}}
        ) from exc
    except AuthorizationExpired as exc:
        body = {"error": {"code": "authorization_expired"}}
        store.idempotency[idempotency_key] = (409, body)
        raise HTTPException(status_code=409, detail=body) from exc
    except CaptureFailed as exc:
        body = {"error": {"code": "capture_failed"}}
        # Not cached: ADR-0008's retryable-capture scenario needs each
        # retry (same Idempotency-Key, since orders->payments->provider
        # reuses one key per operation attempt) to actually re-run the
        # attempt counter, not replay a stale failure forever.
        raise HTTPException(status_code=503, detail=body) from exc
    store.idempotency[idempotency_key] = (status_code, body)
    return body


def _intent_out(intent: store_mod.PaymentIntent) -> dict[str, Any]:
    return {
        "id": intent.id,
        "status": intent.status,
        "amount_minor": intent.amount_minor,
        "currency": intent.currency,
        "sequence": intent.sequence,
    }


class CreateIntentRequest(BaseModel):
    model_config = {"extra": "forbid"}
    amount_minor: int = Field(ge=1)
    currency: str = Field(min_length=3, max_length=3)
    payment_method_token: str
    metadata: dict[str, str] = Field(default_factory=dict)


@router.post("/v1/payment_intents", status_code=200)
async def create_intent(
    body: CreateIntentRequest,
    request: Request,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    store: Store = Depends(_store),
    delay_scale: float = Depends(_delay_scale),
) -> dict[str, Any]:
    def run() -> tuple[int, dict[str, Any]]:
        intent = store.create_intent(
            amount_minor=body.amount_minor,
            currency=body.currency,
            payment_method_token=body.payment_method_token,
            delay_scale=delay_scale,
            metadata=body.metadata,
        )
        return 200, _intent_out(intent)

    return _replay_or_run(store, idempotency_key, run)


@router.get("/v1/payment_intents/{intent_id}")
async def get_intent(intent_id: str, store: Store = Depends(_store)) -> dict[str, Any]:
    try:
        intent = store.get_intent(intent_id)
    except IntentNotFound as exc:
        raise HTTPException(status_code=404) from exc
    return _intent_out(intent)


@router.post("/v1/payment_intents/{intent_id}/capture")
async def capture_intent(
    intent_id: str,
    request: Request,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    store: Store = Depends(_store),
    delay_scale: float = Depends(_delay_scale),
) -> dict[str, Any]:
    def run() -> tuple[int, dict[str, Any]]:
        intent = store.capture(intent_id, delay_scale=delay_scale)
        return 200, _intent_out(intent)

    return _replay_or_run(store, idempotency_key, run)


@router.post("/v1/payment_intents/{intent_id}/cancel")
async def cancel_intent(
    intent_id: str,
    request: Request,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    store: Store = Depends(_store),
    delay_scale: float = Depends(_delay_scale),
) -> dict[str, Any]:
    def run() -> tuple[int, dict[str, Any]]:
        intent = store.void(intent_id, delay_scale=delay_scale)
        return 200, _intent_out(intent)

    return _replay_or_run(store, idempotency_key, run)


class RefundRequest(BaseModel):
    model_config = {"extra": "forbid"}
    amount_minor: int = Field(ge=1)


@router.post("/v1/payment_intents/{intent_id}/refunds")
async def refund_intent(
    intent_id: str,
    body: RefundRequest,
    request: Request,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    store: Store = Depends(_store),
    delay_scale: float = Depends(_delay_scale),
) -> dict[str, Any]:
    def run() -> tuple[int, dict[str, Any]]:
        refund = store.refund(intent_id, amount_minor=body.amount_minor, delay_scale=delay_scale)
        return 200, {"id": refund.id, "payment_intent": intent_id, "status": refund.status}

    return _replay_or_run(store, idempotency_key, run)


# --- non-production control API (LLD-06 §8.1) -------------------------------


@router.post("/__mock/reset")
async def reset(store: Store = Depends(_store)) -> dict[str, str]:
    store.reset()
    return {"status": "reset"}


@router.post("/__mock/payment_intents/{intent_id}:trigger_webhook")
async def trigger_webhook(
    intent_id: str, event_type: str, request: Request, store: Store = Depends(_store)
) -> dict[str, str]:
    """Fires a specific pending webhook now instead of waiting out its real
    delay (15s/60s) -- what tests use instead of sleeping."""
    try:
        webhook = store.force_webhook(intent_id, event_type)
    except IntentNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await request.app.state.webhook_sender.deliver(webhook)
    return {"status": "delivered"}


@router.post("/__mock/payment_intents/{intent_id}:set_always_fail_capture")
async def set_always_fail_capture(intent_id: str, store: Store = Depends(_store)) -> dict[str, str]:
    """LLD-06 §8.1's "always fails, via a scenario flag"."""
    try:
        intent = store.get_intent(intent_id)
    except IntentNotFound as exc:
        raise HTTPException(status_code=404) from exc
    intent.always_fail_capture = True
    return {"status": "ok"}
