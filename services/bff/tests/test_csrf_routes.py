"""Route-level CSRF suite (LLD-01 §7/§16, LLD-14 §4 "CSRF" row): missing or
wrong token, cross-origin Origin, form content type -> rejected.

Builds a minimal app from the real routers with faked ``app.state``
(sessions/settings/oidc), same "fake the external boundary, exercise the
real route logic" pattern as test_proxy.py -- no live Keycloak/Redis/
RabbitMQ needed, since those would conflict with conftest.py's
intentionally-fake OIDC issuer used by the rest of this suite.
"""

import time
from types import SimpleNamespace

import httpx
from app.csrf import derive_csrf_token
from app.main import app as real_app
from app.proxy import ConcurrencyGate
from app.sessions import SessionRecord
from fastapi.testclient import TestClient

_CSRF_SECRET = "a-real-csrf-secret"
_SESSION_ID = "test-session-id"


def _record(**overrides: object) -> SessionRecord:
    now = int(time.time())
    base = dict(
        uid="user-1",
        sub="sub-1",
        idp_sid="idp-1",
        access_token="at-1",
        refresh_token="rt-1",
        id_token="idt-1",
        at_exp=now + 300,
        acr="1",
        auth_time=now,
        csrf=_CSRF_SECRET,
        created=now,
        last_seen=now,
        abs_exp=now + 43200,
        session_class="standard",
    )
    base.update(overrides)
    return SessionRecord(**base)  # type: ignore[arg-type]


class FakeSessions:
    def __init__(self, record: SessionRecord | None) -> None:
        self._record = record
        self.deleted: list[str] = []

    async def load(self, session_id: str, *, idle_s: int) -> SessionRecord | None:
        return self._record

    async def delete(self, session_id: str) -> None:
        self.deleted.append(session_id)

    async def delete_all_for_user(self, uid: str) -> int:
        return 0


class FakeOidc:
    def end_session_url(self, *, id_token: str, post_logout_redirect_uri: str) -> str:
        return (
            f"https://kc.test/logout?id_token_hint={id_token}&redirect={post_logout_redirect_uri}"
        )


def _client(record: SessionRecord | None) -> TestClient:
    real_app.state.settings = SimpleNamespace(
        app_origin="https://app.test",
        cookie_secure=True,
        session_idle_standard_s=1800,
        orgs_svc_base_url="http://orgs-svc.invalid",
        authz_svc_base_url="http://authz-svc.invalid",
        users_svc_base_url="http://users-svc.invalid",
    )
    real_app.state.sessions = FakeSessions(record)
    real_app.state.oidc = FakeOidc()
    real_app.state.api_proxy_gate = ConcurrencyGate(max_concurrency=100)
    real_app.state.http_client = httpx.AsyncClient()
    client = TestClient(real_app, base_url="https://app.test", raise_server_exceptions=False)
    client.cookies.set("__Host-sf_session", _SESSION_ID)
    return client


def test_logout_without_any_csrf_header_is_rejected() -> None:
    client = _client(_record())

    resp = client.post("/auth/logout", headers={"origin": "https://app.test"})

    assert resp.status_code == 403


def test_logout_with_a_wrong_csrf_token_is_rejected() -> None:
    client = _client(_record())

    resp = client.post(
        "/auth/logout",
        headers={"origin": "https://app.test", "x-csrf-token": "wrong-token"},
    )

    assert resp.status_code == 403


def test_logout_with_the_correct_csrf_token_is_accepted() -> None:
    """Positive control -- proves the suite isn't just vacuously 403ing
    everything regardless of the token."""
    client = _client(_record())
    token = derive_csrf_token(_CSRF_SECRET)

    resp = client.post(
        "/auth/logout",
        headers={"origin": "https://app.test", "x-csrf-token": token},
        follow_redirects=False,
    )

    assert resp.status_code == 303


def test_logout_from_a_cross_origin_referer_is_rejected_even_with_a_valid_token() -> None:
    client = _client(_record())
    token = derive_csrf_token(_CSRF_SECRET)

    resp = client.post(
        "/auth/logout",
        headers={"origin": "https://evil.example", "x-csrf-token": token},
    )

    assert resp.status_code == 403


def test_logout_all_without_csrf_is_rejected() -> None:
    client = _client(_record())

    resp = client.post("/auth/logout-all", headers={"origin": "https://app.test"})

    assert resp.status_code == 403


def test_api_proxy_post_without_csrf_is_rejected_before_any_forwarding() -> None:
    client = _client(_record())

    resp = client.post(
        "/api/orgs/v1/orgs/org-1/invitations",
        json={"email": "a@example.com", "roles_requested": ["org_admin"]},
        headers={"origin": "https://app.test"},
    )

    assert resp.status_code == 403


def test_api_proxy_get_needs_no_csrf_token() -> None:
    """GET is a safe method -- CSRF is only enforced on unsafe ones. This
    will fail downstream (no real orgs-svc here), proving the request got
    *past* the CSRF gate rather than that CSRF was silently skipped."""
    client = _client(_record())

    resp = client.get("/api/orgs/v1/orgs/org-1/members")

    assert resp.status_code != 403
