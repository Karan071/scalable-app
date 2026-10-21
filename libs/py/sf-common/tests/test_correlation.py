from fastapi import FastAPI
from fastapi.testclient import TestClient
from sf_common import CorrelationIdMiddleware, get_correlation_id


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(CorrelationIdMiddleware)

    @app.get("/probe")
    async def probe() -> dict[str, str | None]:
        return {"correlation_id": get_correlation_id()}

    return app


def test_generates_a_correlation_id_when_absent() -> None:
    client = TestClient(_build_app())

    response = client.get("/probe")

    assert response.status_code == 200
    body_id = response.json()["correlation_id"]
    assert body_id is not None
    assert response.headers["x-correlation-id"] == body_id


def test_echoes_a_well_formed_client_supplied_correlation_id() -> None:
    client = TestClient(_build_app())
    supplied = "3fa85f64-5717-4562-b3fc-2c963f66afa6"

    response = client.get("/probe", headers={"x-correlation-id": supplied})

    assert response.json()["correlation_id"] == supplied
    assert response.headers["x-correlation-id"] == supplied


def test_replaces_a_malformed_client_supplied_correlation_id() -> None:
    client = TestClient(_build_app())

    response = client.get("/probe", headers={"x-correlation-id": "not-a-uuid"})

    assert response.json()["correlation_id"] != "not-a-uuid"


def test_get_correlation_id_is_none_outside_a_request() -> None:
    assert get_correlation_id() is None
