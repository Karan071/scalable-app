import time

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sf_authz import (
    AuthenticatedUser,
    Internal,
    Principal,
    Public,
    TokenValidator,
    install_token_validator,
)

ISSUER = "https://kc.test/realms/scaleforge"


def _claims(**overrides: object) -> dict[str, object]:
    now = int(time.time())
    base = {
        "iss": ISSUER,
        "sub": "svc:bff",
        "aud": "scaleforge-internal",
        "iat": now,
        "exp": now + 300,
    }
    base.update(overrides)
    return base


def _app(jwks_server: str) -> FastAPI:
    app = FastAPI()
    install_token_validator(app, TokenValidator(issuer=ISSUER, jwks_uri=jwks_server))

    @app.post("/internal/v1/users:provision")
    def provision(principal: Principal = Depends(Internal("users:provision"))) -> dict[str, str]:
        return {"subject": principal.subject}

    @app.get("/v1/me")
    def me(principal: Principal = Depends(AuthenticatedUser())) -> dict[str, str]:
        return {"subject": principal.subject}

    @app.get("/healthz")
    def health(_: None = Depends(Public(rate_class="public"))) -> dict[str, str]:
        return {"status": "ok"}

    return app


def test_internal_route_accepts_token_with_required_scope(jwks_server: str, sign_token) -> None:
    client = TestClient(_app(jwks_server))
    token = sign_token(_claims(scope="users:provision"))

    response = client.post(
        "/internal/v1/users:provision", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200


def test_internal_route_rejects_missing_scope(jwks_server: str, sign_token) -> None:
    client = TestClient(_app(jwks_server))
    token = sign_token(_claims(scope="users:read"))

    response = client.post(
        "/internal/v1/users:provision", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 403


def test_internal_route_rejects_user_audience_token(jwks_server: str, sign_token) -> None:
    client = TestClient(_app(jwks_server))
    token = sign_token(_claims(aud="scaleforge-api", scope="users:provision"))

    response = client.post(
        "/internal/v1/users:provision", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 401


def test_internal_route_rejects_missing_bearer_header(jwks_server: str) -> None:
    client = TestClient(_app(jwks_server))

    response = client.post("/internal/v1/users:provision")

    assert response.status_code == 401


def test_authenticated_user_route_accepts_user_token(jwks_server: str, sign_token) -> None:
    client = TestClient(_app(jwks_server))
    token = sign_token(_claims(aud="scaleforge-api", sub="user:0192"))

    response = client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json() == {"subject": "user:0192"}


def test_public_route_needs_no_token(jwks_server: str) -> None:
    client = TestClient(_app(jwks_server))

    response = client.get("/healthz")

    assert response.status_code == 200
