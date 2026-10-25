"""No database, no Postgres dependency -- this service is in-memory only,
so these tests never skip. LLD-06 §8.1's scenario catalog, one test per
magic token.
"""

from collections.abc import Iterator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client() -> Iterator[TestClient]:
    from app.main import app

    with TestClient(app) as c:
        yield c
    app.state.store.reset()


def _key() -> str:
    return str(uuid4())


def test_pm_mock_success_authorizes_immediately(client: TestClient) -> None:
    resp = client.post(
        "/v1/payment_intents",
        headers={"Idempotency-Key": _key()},
        json={"amount_minor": 1000, "currency": "EUR", "payment_method_token": "pm_mock_success"},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "authorized"


def test_pm_mock_decline_is_402(client: TestClient) -> None:
    resp = client.post(
        "/v1/payment_intents",
        headers={"Idempotency-Key": _key()},
        json={"amount_minor": 1000, "currency": "EUR", "payment_method_token": "pm_mock_decline"},
    )

    assert resp.status_code == 402, resp.text
    assert resp.json()["detail"]["error"]["code"] == "card_declined"


def test_pm_mock_delayed_webhook_starts_processing(client: TestClient) -> None:
    resp = client.post(
        "/v1/payment_intents",
        headers={"Idempotency-Key": _key()},
        json={
            "amount_minor": 1000,
            "currency": "EUR",
            "payment_method_token": "pm_mock_delayed_webhook",
        },
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "processing"


def test_create_intent_is_idempotent_on_replay(client: TestClient) -> None:
    key = _key()
    body = {"amount_minor": 1000, "currency": "EUR", "payment_method_token": "pm_mock_success"}

    first = client.post("/v1/payment_intents", headers={"Idempotency-Key": key}, json=body)
    second = client.post("/v1/payment_intents", headers={"Idempotency-Key": key}, json=body)

    assert first.json()["id"] == second.json()["id"]


def test_capture_then_get_reflects_captured(client: TestClient) -> None:
    created = client.post(
        "/v1/payment_intents",
        headers={"Idempotency-Key": _key()},
        json={"amount_minor": 1000, "currency": "EUR", "payment_method_token": "pm_mock_success"},
    ).json()

    capture = client.post(
        f"/v1/payment_intents/{created['id']}/capture", headers={"Idempotency-Key": _key()}
    )

    assert capture.status_code == 200, capture.text
    assert capture.json()["status"] == "captured"
    assert client.get(f"/v1/payment_intents/{created['id']}").json()["status"] == "captured"


def test_pm_mock_capture_fail_fails_twice_then_succeeds(client: TestClient) -> None:
    created = client.post(
        "/v1/payment_intents",
        headers={"Idempotency-Key": _key()},
        json={
            "amount_minor": 1000,
            "currency": "EUR",
            "payment_method_token": "pm_mock_capture_fail",
        },
    ).json()

    first = client.post(
        f"/v1/payment_intents/{created['id']}/capture", headers={"Idempotency-Key": _key()}
    )
    second = client.post(
        f"/v1/payment_intents/{created['id']}/capture", headers={"Idempotency-Key": _key()}
    )
    third = client.post(
        f"/v1/payment_intents/{created['id']}/capture", headers={"Idempotency-Key": _key()}
    )

    assert first.status_code == 503, first.text
    assert second.status_code == 503, second.text
    assert third.status_code == 200, third.text
    assert third.json()["status"] == "captured"


def test_always_fail_capture_flag_never_succeeds(client: TestClient) -> None:
    created = client.post(
        "/v1/payment_intents",
        headers={"Idempotency-Key": _key()},
        json={
            "amount_minor": 1000,
            "currency": "EUR",
            "payment_method_token": "pm_mock_capture_fail",
        },
    ).json()
    client.post(f"/__mock/payment_intents/{created['id']}:set_always_fail_capture")

    resp = client.post(
        f"/v1/payment_intents/{created['id']}/capture", headers={"Idempotency-Key": _key()}
    )

    assert resp.status_code == 503, resp.text


def test_auth_expires_capture_after_expiry_is_409(client: TestClient) -> None:
    import time

    from app.main import app as fastapi_app

    created = client.post(
        "/v1/payment_intents",
        headers={"Idempotency-Key": _key()},
        json={
            "amount_minor": 1000,
            "currency": "EUR",
            "payment_method_token": "pm_mock_auth_expires",
        },
    ).json()
    # Force expiry without sleeping 2 real minutes.
    intent = fastapi_app.state.store.get_intent(created["id"])
    intent.expires_at = time.monotonic() - 1

    resp = client.post(
        f"/v1/payment_intents/{created['id']}/capture", headers={"Idempotency-Key": _key()}
    )

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["error"]["code"] == "authorization_expired"


def test_void_then_get_reflects_voided(client: TestClient) -> None:
    created = client.post(
        "/v1/payment_intents",
        headers={"Idempotency-Key": _key()},
        json={"amount_minor": 1000, "currency": "EUR", "payment_method_token": "pm_mock_success"},
    ).json()

    resp = client.post(
        f"/v1/payment_intents/{created['id']}/cancel", headers={"Idempotency-Key": _key()}
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "voided"


def test_refund_is_idempotent(client: TestClient) -> None:
    created = client.post(
        "/v1/payment_intents",
        headers={"Idempotency-Key": _key()},
        json={"amount_minor": 1000, "currency": "EUR", "payment_method_token": "pm_mock_success"},
    ).json()
    client.post(f"/v1/payment_intents/{created['id']}/capture", headers={"Idempotency-Key": _key()})

    first = client.post(
        f"/v1/payment_intents/{created['id']}/refunds",
        headers={"Idempotency-Key": _key()},
        json={"amount_minor": 1000},
    )
    second = client.post(
        f"/v1/payment_intents/{created['id']}/refunds",
        headers={"Idempotency-Key": _key()},
        json={"amount_minor": 1000},
    )

    assert first.json()["id"] == second.json()["id"]


def test_trigger_webhook_control_api_fires_on_demand(client: TestClient) -> None:
    created = client.post(
        "/v1/payment_intents",
        headers={"Idempotency-Key": _key()},
        json={
            "amount_minor": 1000,
            "currency": "EUR",
            "payment_method_token": "pm_mock_delayed_webhook",
        },
    ).json()

    resp = client.post(
        f"/__mock/payment_intents/{created['id']}:trigger_webhook?event_type=payment_intent.authorized"
    )

    assert resp.status_code == 200, resp.text


def test_out_of_order_schedules_captured_before_authorized(client: TestClient) -> None:
    from app.main import app as fastapi_app

    created = client.post(
        "/v1/payment_intents",
        headers={"Idempotency-Key": _key()},
        json={
            "amount_minor": 1000,
            "currency": "EUR",
            "payment_method_token": "pm_mock_out_of_order",
        },
    ).json()

    pending = [w for w in fastapi_app.state.store.pending_webhooks if w.intent_id == created["id"]]
    captured = next(w for w in pending if w.event_type == "payment_intent.captured")
    authorized = next(w for w in pending if w.event_type == "payment_intent.authorized")
    # Fires first, but carries the *lower* sequence -- authorization still
    # logically happened first; only its webhook is delayed. A consumer
    # guarding on sequence (not arrival order) must see `authorized` as
    # stale once `captured` (sequence 2) has already applied.
    assert captured.fire_at < authorized.fire_at
    assert captured.sequence == 2
    assert authorized.sequence == 1


def test_reset_clears_everything(client: TestClient) -> None:
    client.post(
        "/v1/payment_intents",
        headers={"Idempotency-Key": _key()},
        json={"amount_minor": 1000, "currency": "EUR", "payment_method_token": "pm_mock_success"},
    )

    resp = client.post("/__mock/reset")

    assert resp.status_code == 200, resp.text
    from app.main import app as fastapi_app

    assert fastapi_app.state.store.intents == {}
