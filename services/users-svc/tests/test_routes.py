"""Integration tests against the local Compose Postgres (`make dev`). Skip if unreachable."""

from collections.abc import Iterator
from uuid import UUID, uuid4, uuid7

import pytest
from fastapi.testclient import TestClient
from sf_authz import Principal


@pytest.fixture
def client() -> Iterator[TestClient]:
    try:
        from app.main import app
    except Exception as exc:
        pytest.skip(f"could not import app.main: {exc}")

    from app.routes import require_provision_scope, require_read_scope

    def fake_internal(scope: str) -> Principal:
        return Principal(
            subject="svc:bff", audiences=("scaleforge-internal",), claims={"scope": scope}
        )

    app.dependency_overrides[require_provision_scope] = lambda: fake_internal("users:provision")
    app.dependency_overrides[require_read_scope] = lambda: fake_internal("users:read")

    try:
        with TestClient(app) as c:
            # exercise a trivial query to fail fast (and clearly) if Postgres is down
            resp = c.get("/readyz")
            if resp.status_code != 200:
                pytest.skip(f"users-svc not ready (DB unreachable?): {resp.json()}")
            yield c
    except Exception as exc:
        pytest.skip(f"local Postgres not reachable: {exc}")
    finally:
        app.dependency_overrides.clear()


def _as_user(client: TestClient, sf_uid: UUID) -> None:
    from app.main import app
    from app.routes import require_authenticated_user

    app.dependency_overrides[require_authenticated_user] = lambda: Principal(
        subject=f"user:{sf_uid}", audiences=("scaleforge-api",), claims={"sf_uid": str(sf_uid)}
    )


def _unique_identity() -> dict[str, object]:
    token = uuid7().hex
    return {
        "idp_issuer": "https://kc.test/realms/scaleforge",
        "idp_subject": f"subject-{token}",
        "email": f"user-{token}@example.com",
        "email_verified": True,
    }


def test_provision_creates_a_new_user(client: TestClient) -> None:
    body = _unique_identity()

    response = client.post("/internal/v1/users:provision", json=body)

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "active"
    assert data["needs_claim_refresh"] is True
    UUID(data["user_id"])  # does not raise


def test_provision_is_idempotent(client: TestClient) -> None:
    body = _unique_identity()

    first = client.post("/internal/v1/users:provision", json=body).json()
    second = client.post("/internal/v1/users:provision", json=body).json()

    assert first["user_id"] == second["user_id"]
    assert second["needs_claim_refresh"] is False


def test_get_me_returns_the_provisioned_profile(client: TestClient) -> None:
    body = _unique_identity()
    provisioned = client.post("/internal/v1/users:provision", json=body).json()
    _as_user(client, UUID(provisioned["user_id"]))

    response = client.get("/v1/me")

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == provisioned["user_id"]
    assert data["email"].lower() == str(body["email"]).lower()
    assert data["status"] == "active"


def test_patch_me_updates_display_name(client: TestClient) -> None:
    body = _unique_identity()
    provisioned = client.post("/internal/v1/users:provision", json=body).json()
    _as_user(client, UUID(provisioned["user_id"]))

    response = client.patch("/v1/me", json={"display_name": "New Name"})

    assert response.status_code == 200
    assert response.json()["display_name"] == "New Name"
    assert response.json()["version"] == 2


def test_patch_me_claims_an_available_handle(client: TestClient) -> None:
    body = _unique_identity()
    provisioned = client.post("/internal/v1/users:provision", json=body).json()
    _as_user(client, UUID(provisioned["user_id"]))
    handle = f"h{uuid4().hex[:10]}"

    response = client.patch("/v1/me", json={"handle": handle})

    assert response.status_code == 200
    assert response.json()["handle"] == handle


def test_patch_me_rejects_a_reserved_handle(client: TestClient) -> None:
    body = _unique_identity()
    provisioned = client.post("/internal/v1/users:provision", json=body).json()
    _as_user(client, UUID(provisioned["user_id"]))

    response = client.patch("/v1/me", json={"handle": "admin"})

    assert response.status_code == 409


def test_handle_availability_reports_taken_and_free(client: TestClient) -> None:
    body = _unique_identity()
    provisioned = client.post("/internal/v1/users:provision", json=body).json()
    _as_user(client, UUID(provisioned["user_id"]))
    handle = f"h{uuid4().hex[:10]}"
    client.patch("/v1/me", json={"handle": handle})

    taken = client.get("/v1/me/handle-availability", params={"handle": handle})
    free = client.get("/v1/me/handle-availability", params={"handle": f"h{uuid4().hex[:10]}"})

    assert taken.json() == {"available": False}
    assert free.json() == {"available": True}


def test_batch_returns_only_requested_ids(client: TestClient) -> None:
    first = client.post("/internal/v1/users:provision", json=_unique_identity()).json()
    second = client.post("/internal/v1/users:provision", json=_unique_identity()).json()

    response = client.get(
        "/internal/v1/users:batch", params={"ids": [first["user_id"], uuid7().hex]}
    )

    assert response.status_code == 200
    returned_ids = {row["id"] for row in response.json()}
    assert returned_ids == {first["user_id"]}
    assert second["user_id"] not in returned_ids


def _as_platform_admin(client: TestClient) -> None:
    from app.main import app
    from app.routes import require_manage_platform_user, require_read_platform_user

    principal = Principal(
        subject=f"user:{uuid7()}", audiences=("scaleforge-api",), claims={"acr": 2}
    )
    app.dependency_overrides[require_read_platform_user] = lambda: principal
    app.dependency_overrides[require_manage_platform_user] = lambda: principal


def test_list_platform_users_finds_by_search(client: TestClient) -> None:
    body = _unique_identity()
    provisioned = client.post("/internal/v1/users:provision", json=body).json()
    _as_platform_admin(client)

    response = client.get("/v1/platform/users", params={"search": str(body["email"]).split("@")[0]})

    assert response.status_code == 200
    ids = {row["id"] for row in response.json()}
    assert provisioned["user_id"] in ids


def test_disable_then_reactivate_platform_user(client: TestClient) -> None:
    provisioned = client.post("/internal/v1/users:provision", json=_unique_identity()).json()
    _as_platform_admin(client)

    disable_resp = client.post(
        f"/v1/platform/users/{provisioned['user_id']}:disable",
        json={"reason": "fraud investigation"},
    )
    assert disable_resp.status_code == 200, disable_resp.text
    assert disable_resp.json() == {"status": "disabled"}

    reactivate_resp = client.post(f"/v1/platform/users/{provisioned['user_id']}:reactivate")
    assert reactivate_resp.status_code == 200, reactivate_resp.text
    assert reactivate_resp.json() == {"status": "active"}


def test_disable_platform_user_requires_reason(client: TestClient) -> None:
    provisioned = client.post("/internal/v1/users:provision", json=_unique_identity()).json()
    _as_platform_admin(client)

    response = client.post(
        f"/v1/platform/users/{provisioned['user_id']}:disable", json={"reason": "short"}
    )

    assert response.status_code == 422


def test_disable_platform_user_404_for_unknown_user(client: TestClient) -> None:
    _as_platform_admin(client)

    response = client.post(
        f"/v1/platform/users/{uuid7()}:disable", json={"reason": "fraud investigation"}
    )

    assert response.status_code == 404
