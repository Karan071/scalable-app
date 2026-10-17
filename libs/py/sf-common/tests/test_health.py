from fastapi import FastAPI
from fastapi.testclient import TestClient
from sf_common import build_health_router


def test_livez_never_checks_dependencies() -> None:
    app = FastAPI()
    app.include_router(build_health_router(readiness_checks=[_always_false]))

    response = TestClient(app).get("/livez")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_fails_when_a_check_fails() -> None:
    app = FastAPI()
    app.include_router(build_health_router(readiness_checks=[_always_false]))

    response = TestClient(app).get("/readyz")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}


def test_readyz_ok_with_no_checks() -> None:
    app = FastAPI()
    app.include_router(build_health_router())

    response = TestClient(app).get("/readyz")

    assert response.status_code == 200


async def _always_false() -> bool:
    return False
