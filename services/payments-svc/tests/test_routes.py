"""Integration tests against the local Compose Postgres (`make test-env`).
Skip if unreachable. The HTTP layer: the internal authorize endpoint's
status-code mapping, and the webhook endpoint's signature gate.
"""

import hashlib
import hmac
import json
import time
from collections.abc import Iterator
from uuid import uuid4, uuid7

import pytest
from fakes import FakeProviderClient
from fastapi.testclient import TestClient
from sf_authz import Principal

_SECRET = "test-webhook-secret"


@pytest.fixture
def client() -> Iterator[TestClient]:
    try:
        from app.main import app
    except Exception as exc:
        pytest.skip(f"could not import app.main: {exc}")

    from app import routes

    internal = Principal(
        subject="svc:orders",
        audiences=("scaleforge-internal",),
        claims={"scope": "payments:authorize"},
    )
    app.dependency_overrides[routes.require_authorize_scope] = lambda: internal

    try:
        with TestClient(app) as c:
            resp = c.get("/readyz")
            if resp.status_code != 200:
                pytest.skip(f"payments-svc not ready (DB unreachable?): {resp.json()}")
            app.state.provider = FakeProviderClient()
            yield c
    except Exception as exc:
        pytest.skip(f"local Postgres not reachable: {exc}")
    finally:
        app.dependency_overrides.clear()


def _checkout_body() -> dict:
    return {
        "order_id": str(uuid7()),
        "org_id": str(uuid4()),
        "customer_id": str(uuid7()),
        "amount_minor": 1500,
        "currency": "EUR",
        "payment_method_token": "pm_mock_success",
    }


def test_authorize_success_is_200(client: TestClient) -> None:
    resp = client.post("/internal/v1/payments", json=_checkout_body())

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "authorized"


def test_authorize_decline_is_402(client: TestClient) -> None:
    body = _checkout_body()
    body["payment_method_token"] = "pm_mock_decline"

    resp = client.post("/internal/v1/payments", json=body)

    assert resp.status_code == 402, resp.text


def test_authorize_unknown_is_202(client: TestClient) -> None:
    client.app.state.provider.force_authorize_timeout("pm_mock_timeout")
    body = _checkout_body()
    body["payment_method_token"] = "pm_mock_timeout"

    resp = client.post("/internal/v1/payments", json=body)

    assert resp.status_code == 202, resp.text
    assert resp.json()["status"] == "unknown"


def _sign(body: bytes) -> str:
    t = int(time.time())
    signature = hmac.new(_SECRET.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={t},v1={signature}"


def _webhook_event(intent_id: str) -> dict:
    return {
        "provider_event_id": f"evt_{uuid7()}",
        "type": "payment_intent.authorized",
        "data": {
            "object": {
                "id": intent_id,
                "status": "authorized",
                "amount_minor": 1500,
                "currency": "EUR",
                "sequence": 1,
                "metadata": {},
            }
        },
    }


def test_webhook_rejects_a_missing_signature(client: TestClient) -> None:
    body = json.dumps(_webhook_event("intent_x")).encode()

    resp = client.post(
        "/webhooks/payments/mock", content=body, headers={"content-type": "application/json"}
    )

    assert resp.status_code == 400, resp.text


def test_webhook_rejects_a_bad_signature(client: TestClient) -> None:
    body = json.dumps(_webhook_event("intent_x")).encode()

    resp = client.post(
        "/webhooks/payments/mock",
        content=body,
        headers={"content-type": "application/json", "sf-signature": "t=1,v1=deadbeef"},
    )

    assert resp.status_code == 400, resp.text


def test_webhook_with_a_valid_signature_but_unmatched_intent_is_accepted(
    client: TestClient,
) -> None:
    body = json.dumps(_webhook_event("unmatched_intent")).encode()

    resp = client.post(
        "/webhooks/payments/mock",
        content=body,
        headers={"content-type": "application/json", "sf-signature": _sign(body)},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["result"] == "ignored"
