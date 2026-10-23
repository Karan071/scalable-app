"""M2 exit evidence: login through the BFF against the real Keycloak (LLD-01 §16).

    make test-system

Real users-svc, orgs-svc, authz-svc and two BFF replicas on the test
environment, with the Compose Keycloak and its realm-as-code. Each test
creates its own Keycloak users and deletes them afterwards. Covers:

- MFA at login for the ``mfa-required`` group, opt-in for everyone else,
  and step-up to ``acr=2`` with a single OTP (LLD-01 §3.3, §9).
- Sessions across 2 replicas; logout on one is rejected on the other.
- No token material in anything the BFF sends the browser, and none
  readable in Redis.
- Admin disable revokes every session within LLD-01 §8's 5 s target and
  blocks the next Keycloak login.

Needs a fresh test DB (`make test-env`, which `make test-system` runs):
the admin test bootstraps the one ``super_user``. Skips if Keycloak isn't up.
"""

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID

import httpx
import pytest
import pytest_asyncio
from harness import ServiceProcess, bootstrap_super_user, service_env, wait_until
from keycloak import (
    APP_ORIGIN,
    CALLBACK,
    KC,
    Browser,
    KcUser,
    KeycloakAdmin,
    KeycloakIdP,
    SignIn,
    contains_jwt,
)
from redis.asyncio import Redis

pytestmark = pytest.mark.asyncio(loop_scope="module")

_SESSION_REDIS_URL = "redis://localhost:6379/1"
_SESSION_KEYS = '{"k1": "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="}'
_PORTS = {"users-svc": 18131, "orgs-svc": 18132, "authz-svc": 18133}
_BFF_PORTS = (18134, 18135)

#: LLD-01 §8: "Admin disables a user ... ≤ 5 s target".
_DISABLE_REVOCATION_TARGET_S = 5.0


@dataclass
class Stack:
    http: httpx.AsyncClient
    kc: KeycloakAdmin
    bff_replicas: list[str]
    users: list[KcUser]

    async def user(self, *, mfa_required: bool = False) -> KcUser:
        user = await self.kc.create_user(groups=("/mfa-required",) if mfa_required else ())
        self.users.append(user)
        return user

    def browser(self) -> Browser:
        return Browser(self.http, self.bff_replicas)


def _bff_env(idp: KeycloakIdP) -> dict[str, str]:
    from harness import ENV

    return service_env(
        "bff",
        idp,
        SF_OIDC_CLIENT_ID="sf-bff",
        SF_OIDC_CLIENT_SECRET=ENV["SF_BFF_CLIENT_SECRET"],
        SF_OIDC_REDIRECT_URI=CALLBACK,
        SF_APP_ORIGIN=APP_ORIGIN,
        SF_COOKIE_SECURE="false",  # plain http here; production is always Secure
        SF_SVC_BFF_CLIENT_SECRET=ENV["SF_SVC_BFF_CLIENT_SECRET"],
        SF_USERS_SVC_BASE_URL=f"http://127.0.0.1:{_PORTS['users-svc']}",
        SF_ORGS_SVC_BASE_URL=f"http://127.0.0.1:{_PORTS['orgs-svc']}",
        SF_AUTHZ_SVC_BASE_URL=f"http://127.0.0.1:{_PORTS['authz-svc']}",
        SF_REDIS_SESSION_URL=_SESSION_REDIS_URL,
        SF_SESSION_ENC_KEYS=_SESSION_KEYS,
        SF_SESSION_ENC_PRIMARY_KID="k1",
    )


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def stack() -> AsyncIterator[Stack]:
    async with httpx.AsyncClient(timeout=10) as http:
        try:
            (await http.get(f"{KC}/realms/scaleforge")).raise_for_status()
        except httpx.HTTPError as exc:
            pytest.skip(f"Compose Keycloak not reachable: {exc}")

        idp = KeycloakIdP()
        orgs_url = f"http://127.0.0.1:{_PORTS['orgs-svc']}"
        processes = [
            ServiceProcess(
                svc,
                port=port,
                label=f"{svc}-login",
                env=service_env(svc, idp, SF_ORGS_SVC_BASE_URL=orgs_url),
            )
            for svc, port in _PORTS.items()
        ] + [
            ServiceProcess("bff", port=port, label=f"bff-{name}", env=_bff_env(idp))
            for name, port in zip("ab", _BFF_PORTS, strict=True)
        ]
        s = Stack(http, KeycloakAdmin(http), [p.base_url for p in processes[-2:]], [])
        try:
            await asyncio.gather(*(p.start() for p in processes))
            yield s
        finally:
            for p in processes:
                p.stop()
            for user in s.users:
                await s.kc.delete_user(user)


async def _session(browser: Browser, *, replica: int | None = None) -> dict:
    resp = await browser.bff("GET", "/auth/session", replica=replica)
    assert resp.status_code == 200, resp.text
    return resp.json()  # type: ignore[no-any-return]


async def _step_up(browser: Browser, user: KcUser, path: str = "/auth/step-up") -> SignIn:
    # auth_time has 1 s resolution, and the BFF refuses a step-up that isn't
    # strictly newer than the login (LLD-01 §9 step 5). People take longer.
    await asyncio.sleep(1.1)
    return await browser.sign_in(user, path)


def _session_cookie(browser: Browser) -> str:
    return browser.cookies(browser._replicas[0])["sf_session"]


async def test_group_member_must_enroll_mfa_at_login_then_steps_up_with_one_otp(
    stack: Stack,
) -> None:
    user = await stack.user(mfa_required=True)
    browser = stack.browser()

    login = await browser.sign_in(user)

    assert login.pages == ["password", "enroll-otp"]
    assert login.location == "/"
    session = await _session(browser)
    assert session["authenticated"] is True
    # Keycloak 26.0 can't raise acr for MFA done at login (LLD-01 §3.3).
    assert session["acr"] == "1"

    before = _session_cookie(browser)
    step_up = await _step_up(browser, user, "/auth/step-up?return_to=/settings")

    assert step_up.pages == ["otp"]  # the same SSO session: no password again
    assert step_up.location == "/settings"
    assert (await _session(browser))["acr"] == "2"
    # LLD-01 §9 step 5: step-up rotates the session ID; the old one is dead.
    assert _session_cookie(browser) != before
    old = await stack.http.get(
        f"{stack.bff_replicas[0]}/auth/session", headers={"cookie": f"sf_session={before}"}
    )
    assert old.json() == {"authenticated": False}


async def test_users_outside_the_group_sign_in_with_a_password_and_enroll_on_step_up(
    stack: Stack,
) -> None:
    user = await stack.user()
    browser = stack.browser()

    login = await browser.sign_in(user)

    assert login.pages == ["password"]
    assert (await _session(browser))["acr"] == "1"

    step_up = await _step_up(browser, user)

    assert step_up.pages == ["enroll-otp"]
    assert (await _session(browser))["acr"] == "2"

    # Once enrolled, MFA is no longer optional for them at login.
    again = stack.browser()
    assert (await again.sign_in(user)).pages == ["password", "otp"]


async def test_a_session_works_on_both_replicas_and_logout_on_one_ends_it_on_both(
    stack: Stack,
) -> None:
    user = await stack.user()
    browser = stack.browser()
    await browser.sign_in(user)

    for replica in (0, 1, 0, 1):
        assert (await _session(browser, replica=replica))["authenticated"] is True
        me = await browser.bff("GET", "/api/users/v1/me", replica=replica)
        assert me.status_code == 200, me.text

    csrf = (await _session(browser, replica=0))["csrf_token"]
    logout = await browser.bff(
        "POST", "/auth/logout", replica=0, headers={"origin": APP_ORIGIN, "x-csrf-token": csrf}
    )
    assert (logout.status_code, logout.headers["location"]) == (303, "/")
    # Keycloak's session was ended server-side, before the response.
    assert await stack.kc.sessions(user) == []

    assert (await _session(browser, replica=1)) == {"authenticated": False}
    assert (await browser.bff("GET", "/api/users/v1/me", replica=1)).status_code == 401
    # Keycloak's SSO session ended too: signing in asks for the password again.
    assert (await browser.sign_in(user)).pages == ["password"]


async def test_no_token_material_reaches_the_browser(stack: Stack) -> None:
    """LLD-01 §16, API half: every status line, header (cookies included) and
    body the BFF sent during login, step-up, API calls and logout. The
    Playwright half (browser storage) belongs to the web E2E suite."""
    user = await stack.user(mfa_required=True)
    browser = stack.browser()
    await browser.sign_in(user)
    await browser.bff("GET", "/api/users/v1/me")
    await _step_up(browser, user)
    session = await _session(browser)
    await browser.bff("GET", "/api/orgs/v1/me/memberships")

    async with Redis.from_url(_SESSION_REDIS_URL, decode_responses=True) as redis:
        stored = [await redis.hgetall(key) async for key in redis.scan_iter("sess:*")]

    csrf = session["csrf_token"]
    await browser.bff("POST", "/auth/logout", headers={"origin": APP_ORIGIN, "x-csrf-token": csrf})

    assert {r.request.url.path for r in browser.bff_responses} >= {
        "/auth/login",
        "/auth/callback",
        "/auth/step-up",
        "/auth/session",
        "/api/users/v1/me",
        "/auth/logout",
    }
    leaks = [
        f"{r.request.method} {r.request.url.path}"
        for r in browser.bff_responses
        if contains_jwt(str(r.headers.multi_items())) or contains_jwt(r.text)
    ]
    assert leaks == []
    assert set(session) == {
        "authenticated",
        "user",
        "csrf_token",
        "memberships",
        "platform_roles",
        "acr",
        "session_class",
    }
    # At rest: Redis holds the tokens encrypted (LLD-01 §5.5).
    assert stored
    assert not any(contains_jwt(value) for record in stored for value in record.values())


async def test_admin_disable_revokes_every_session_and_blocks_login(stack: Stack) -> None:
    admin, target = await stack.user(), await stack.user()
    admin_browser = stack.browser()
    await admin_browser.sign_in(admin)
    admin_id = (await _session(admin_browser))["user"]["id"]
    bootstrap_super_user(UUID(admin_id))
    await _step_up(admin_browser, admin)  # platform routes need acr 2

    target_browsers = [stack.browser(), stack.browser()]
    for browser in target_browsers:
        await browser.sign_in(target)
    target_id = (await _session(target_browsers[0]))["user"]["id"]

    csrf = (await _session(admin_browser))["csrf_token"]

    async def disable() -> bool:
        # The bootstrap's grant reaches users-svc's policy cache within its
        # 10 s poll; until then the admin is (correctly) forbidden.
        resp = await admin_browser.bff(
            "POST",
            f"/api/users/v1/platform/users/{target_id}:disable",
            headers={"origin": APP_ORIGIN, "x-csrf-token": csrf},
            json={"reason": "system test: admin revocation"},
        )
        assert resp.status_code in (200, 403), resp.text
        return resp.status_code == 200

    await wait_until(disable, timeout_s=20, interval_s=0.5)
    disabled_at = time.monotonic()

    async def all_target_sessions_revoked() -> bool:
        sessions = [await _session(b) for b in target_browsers]
        return all(s == {"authenticated": False} for s in sessions)

    await wait_until(all_target_sessions_revoked, timeout_s=_DISABLE_REVOCATION_TARGET_S)

    async def keycloak_user_disabled() -> bool:
        user = await stack.kc.get_user(target)
        return user["enabled"] is False and await stack.kc.sessions(target) == []

    await wait_until(keycloak_user_disabled, timeout_s=_DISABLE_REVOCATION_TARGET_S)
    assert time.monotonic() - disabled_at <= _DISABLE_REVOCATION_TARGET_S

    retry = await stack.browser().sign_in(target)
    assert retry.location is None
    assert "disabled" in retry.last_page_text.lower()
