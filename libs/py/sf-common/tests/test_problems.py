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

    @app.get("/stale")
    def stale() -> None:
        raise HTTPException(status_code=412, detail="version conflict", headers={"etag": '"v3"'})

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


def test_http_exception_headers_reach_the_response() -> None:
    response = TestClient(_app(), raise_server_exceptions=False).get("/stale")

    assert response.status_code == 412
    assert response.headers["etag"] == '"v3"'


def test_unhandled_exception_becomes_500_problem() -> None:
    response = TestClient(_app(), raise_server_exceptions=False).get("/crash")

    assert response.status_code == 500
    assert response.headers["content-type"] == "application/problem+json"


def test_unknown_route_is_rfc9457_404() -> None:
    response = TestClient(_app(), raise_server_exceptions=False).get("/missing")

    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"


class _PoolTimeout(Exception):
    pass


def _app_with_correlation() -> FastAPI:
    from sf_common import CorrelationIdMiddleware

    app = FastAPI()
    app.add_middleware(CorrelationIdMiddleware)
    install_problem_details(app, db_busy_errors=(_PoolTimeout,))

    @app.get("/boom")
    def boom() -> None:
        raise HTTPException(status_code=409, detail="conflict")

    @app.get("/crash")
    def crash() -> None:
        raise RuntimeError("unexpected")

    @app.get("/busy")
    def busy() -> None:
        raise _PoolTimeout()

    return app


def test_problems_carry_the_client_supplied_correlation_id() -> None:
    """ADR-0015: the id a client can quote to support is in the body too."""
    supplied = "3fa85f64-5717-4562-b3fc-2c963f66afa6"
    response = TestClient(_app_with_correlation()).get(
        "/boom", headers={"x-correlation-id": supplied}
    )

    assert response.json()["correlation_id"] == supplied
    assert response.headers["x-correlation-id"] == supplied


def test_a_500_still_carries_the_generated_correlation_id() -> None:
    """Unhandled errors are rendered outside the correlation middleware's
    context; the id must survive into that response anyway."""
    response = TestClient(_app_with_correlation(), raise_server_exceptions=False).get("/crash")

    assert response.status_code == 500
    body_id = response.json()["correlation_id"]
    assert body_id
    assert response.headers["x-correlation-id"] == body_id


def test_pool_exhaustion_is_503_db_busy_with_retry_after() -> None:
    """LLD-09 §2: 'fail fast -> 503 db-busy'."""
    response = TestClient(_app_with_correlation()).get("/busy")

    assert response.status_code == 503
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["retry-after"] == "1"
    assert response.json()["title"] == "db-busy"
    assert response.json()["correlation_id"]
