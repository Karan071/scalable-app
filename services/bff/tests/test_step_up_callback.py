"""Step-up callback (LLD-01 §9 step 5): verifies ``acr`` and ``auth_time``,
then rotates the session. Same faked-``app.state`` pattern as
test_csrf_routes.py; the real-Keycloak run is tests/system/test_bff_login.py."""

import hashlib
import time
from types import SimpleNamespace

import pytest
from app import pending_login
from app.main import app as real_app
from app.oidc import generate_pkce
from fastapi.testclient import TestClient
from sf_authz import Principal
from test_csrf_routes import FakeSessions, _record

_OLD_SESSION = "pre-step-up-session"
_BINDING = "binding-cookie-value"
_LOGGED_IN_AT = int(time.time()) - 120


class _RotatingSessions(FakeSessions):
    def __init__(self) -> None:
        super().__init__(_record(auth_time=_LOGGED_IN_AT))
        self.created: list[dict] = []

    async def create(self, **fields: object) -> tuple[str, str]:
        self.created.append(fields)
        return "new-session", "csrf"


class _SteppedUpOidc:
    def __init__(self, *, acr: str, auth_time: int) -> None:
        self._claims = {"acr": acr, "auth_time": auth_time, "sid": "idp-1"}

    async def exchange_code(self, *, code: str, code_verifier: str) -> dict[str, object]:
        return {"access_token": "at", "refresh_token": "rt", "id_token": "idt", "expires_in": 300}

    def validate_id_token(self, id_token: str, *, nonce: str | None) -> Principal:
        return Principal(subject="sub-1", audiences=("sf-bff",), claims=self._claims)


def _callback(
    monkeypatch: pytest.MonkeyPatch, *, acr: str, auth_time: int
) -> tuple[str, _RotatingSessions]:
    async def consume(_redis: object, _state: str) -> dict[str, object]:
        return {
            "nonce": "n",
            "verifier": generate_pkce().verifier,
            "return_to": "/settings",
            "binding_hash": hashlib.sha256(_BINDING.encode()).hexdigest(),
            "step_up": True,
        }

    monkeypatch.setattr(pending_login, "consume", consume)
    sessions = _RotatingSessions()
    real_app.state.settings = SimpleNamespace(
        cookie_secure=True, session_idle_standard_s=1800, session_abs_standard_s=43200
    )
    real_app.state.redis = None
    real_app.state.sessions = sessions
    real_app.state.oidc = _SteppedUpOidc(acr=acr, auth_time=auth_time)
    client = TestClient(real_app, base_url="https://app.test")
    client.cookies.set("__Host-sf_session", _OLD_SESSION)
    client.cookies.set("__Host-sf_auth", _BINDING)

    resp = client.get("/auth/callback?code=c&state=s", follow_redirects=False)

    assert resp.status_code == 302
    return resp.headers["location"], sessions


def test_a_fresh_acr_2_authentication_rotates_the_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    location, sessions = _callback(monkeypatch, acr="2", auth_time=int(time.time()))

    assert location == "/settings"
    assert sessions.created[0]["acr"] == "2"
    assert sessions.deleted == [_OLD_SESSION]


@pytest.mark.parametrize(
    ("acr", "auth_time"),
    [
        ("1", int(time.time())),  # MFA prompt cancelled
        ("2", _LOGGED_IN_AT),  # no re-authentication: the session's own login
    ],
)
def test_a_step_up_without_a_fresh_second_factor_is_refused(
    monkeypatch: pytest.MonkeyPatch, acr: str, auth_time: int
) -> None:
    location, sessions = _callback(monkeypatch, acr=acr, auth_time=auth_time)

    assert location == "/auth/error?code=step_up_not_satisfied"
    assert sessions.created == []
    assert sessions.deleted == []
