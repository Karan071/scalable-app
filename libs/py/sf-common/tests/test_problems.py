from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sf_common import install_problem_details


def _app() -> FastAPI:
    app = FastAPI()
    install_problem_details(app)

    @app.get("/boom")
    def boom() -> None:
        raise HTTPException(status_code=409, detail="conflict")

    @app.get("/crash")
    def crash() -> None:
        raise RuntimeError("unexpected")

    return app


def test_http_exception_is_rfc9457() -> None:
    response = TestClient(_app(), raise_server_exceptions=False).get("/boom")

    assert response.status_code == 409
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json() == {
        "type": "about:blank",
        "title": "conflict",
        "status": 409,
        "instance": "/boom",
    }


def test_unhandled_exception_becomes_500_problem() -> None:
    response = TestClient(_app(), raise_server_exceptions=False).get("/crash")

    assert response.status_code == 500
    assert response.headers["content-type"] == "application/problem+json"


def test_unknown_route_is_rfc9457_404() -> None:
    response = TestClient(_app(), raise_server_exceptions=False).get("/missing")

    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
