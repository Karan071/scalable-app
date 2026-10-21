"""Authorize() dependency tests: real signed JWTs (JWKS fixture) + a fake enforcer double."""

import time

from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient
from sf_authz import Authorize, Principal, TokenValidator, install_enforcer, install_token_validator

ISSUER = "https://kc.test/realms/scaleforge"


class _FakeEnforcer:
    """Duck-typed stand-in for ``PolicyEnforcer`` — no DB needed for these tests."""

    def __init__(
        self,
        *,
        allow: bool = True,
        domain_statuses: dict[str, str] | None = None,
        staleness_s: float = 0.0,
    ) -> None:
        self._allow = allow
        self._domain_statuses = domain_statuses or {}
        self._staleness_s = staleness_s
        self.enforce_calls: list[tuple[str, str, str, str]] = []

    def domain_status(self, domain: str) -> str:
        return self._domain_statuses.get(domain, "active")

    def staleness_s(self) -> float:
        return self._staleness_s

    def enforce(self, sub: str, dom: str, obj: str, act: str) -> bool:
        self.enforce_calls.append((sub, dom, obj, act))
        return self._allow


def _claims(**overrides: object) -> dict[str, object]:
    now = int(time.time())
    base = {
        "iss": ISSUER,
        "sub": "user:0192",
        "aud": "scaleforge-api",
        "sf_uid": "0192f1c4-0000-7000-8000-000000000001",
        "iat": now,
        "exp": now + 300,
    }
    base.update(overrides)
    return base


def _org_domain(request: Request) -> str:
    return f"org:{request.path_params['org_id']}"


def _app(jwks_server: str, enforcer: _FakeEnforcer) -> FastAPI:
    app = FastAPI()
    install_token_validator(app, TokenValidator(issuer=ISSUER, jwks_uri=jwks_server))
    install_enforcer(app, enforcer)  # type: ignore[arg-type]

    @app.get("/v1/orgs/{org_id}/members")
    def list_members(
        org_id: str, principal: Principal = Depends(Authorize("member", "read", domain=_org_domain))
    ) -> dict[str, str]:
        return {"org_id": org_id, "subject": principal.subject}

    @app.post("/v1/platform/role-assignments")
    def platform_grant(
        principal: Principal = Depends(
            Authorize("role_assignment", "assign", domain="platform", min_acr=2)
        ),
    ) -> dict[str, str]:
        return {"subject": principal.subject}

    return app


def test_allowed_request_reaches_the_handler(jwks_server: str, sign_token) -> None:
    enforcer = _FakeEnforcer(allow=True)
    client = TestClient(_app(jwks_server, enforcer))
    token = sign_token(_claims())

    response = client.get("/v1/orgs/O1/members", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert enforcer.enforce_calls == [
        ("user:0192f1c4-0000-7000-8000-000000000001", "org:O1", "member", "read")
    ]


def test_denied_request_returns_403_forbidden(jwks_server: str, sign_token) -> None:
    enforcer = _FakeEnforcer(allow=False)
    client = TestClient(_app(jwks_server, enforcer))
    token = sign_token(_claims())

    response = client.get("/v1/orgs/O1/members", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 403
    assert response.json()["detail"] == "forbidden"


def test_domain_resolved_from_path_is_tenant_scoped(jwks_server: str, sign_token) -> None:
    enforcer = _FakeEnforcer(allow=True)
    client = TestClient(_app(jwks_server, enforcer))
    token = sign_token(_claims())

    client.get("/v1/orgs/O2/members", headers={"Authorization": f"Bearer {token}"})

    assert enforcer.enforce_calls[0][1] == "org:O2"


def test_suspended_domain_returns_403_org_suspended(jwks_server: str, sign_token) -> None:
    enforcer = _FakeEnforcer(allow=True, domain_statuses={"org:O1": "suspended"})
    client = TestClient(_app(jwks_server, enforcer))
    token = sign_token(_claims())

    response = client.get("/v1/orgs/O1/members", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 403
    assert response.json()["detail"] == "org_suspended"


def test_stale_policy_past_300s_returns_503(jwks_server: str, sign_token) -> None:
    enforcer = _FakeEnforcer(allow=True, staleness_s=301.0)
    client = TestClient(_app(jwks_server, enforcer))
    token = sign_token(_claims())

    response = client.get("/v1/orgs/O1/members", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 503
    assert response.json()["detail"] == "authz_stale"


def test_platform_route_without_step_up_returns_401(jwks_server: str, sign_token) -> None:
    enforcer = _FakeEnforcer(allow=True)
    client = TestClient(_app(jwks_server, enforcer))
    token = sign_token(_claims(acr=1))  # acr < 2 required for platform domain

    response = client.post(
        "/v1/platform/role-assignments", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "step_up_required"


def test_platform_route_with_step_up_succeeds(jwks_server: str, sign_token) -> None:
    enforcer = _FakeEnforcer(allow=True)
    client = TestClient(_app(jwks_server, enforcer))
    token = sign_token(_claims(acr=2))

    response = client.post(
        "/v1/platform/role-assignments", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200


def test_token_missing_sf_uid_claim_returns_403(jwks_server: str, sign_token) -> None:
    enforcer = _FakeEnforcer(allow=True)
    client = TestClient(_app(jwks_server, enforcer))
    claims = _claims()
    del claims["sf_uid"]
    token = sign_token(claims)

    response = client.get("/v1/orgs/O1/members", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 403
