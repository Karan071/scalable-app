"""Real-Keycloak helpers for browser-login system tests (LLD-01 §16).

The fake IdP in harness.py can't test login: MFA, step-up, and SSO
sessions are Keycloak's own behaviour, configured by the realm-as-code in
deploy/compose/keycloak/. These tests drive the Compose Keycloak the way a
browser does, through the BFF, with throwaway users they delete afterwards.
"""

import hashlib
import hmac
import html
import re
import struct
import time
import uuid
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx
from harness import ENV

KC = "http://localhost:8081"
ISSUER = f"{KC}/realms/scaleforge"
_ADMIN = f"{KC}/admin/realms/scaleforge"

#: What the BFF's registered redirect URI and origin are (realm-scaleforge.json).
#: Nothing listens here during tests: the Browser routes it to a replica,
#: the way a load balancer in front of the replicas would.
APP_ORIGIN = "http://localhost:8020"
CALLBACK = f"{APP_ORIGIN}/auth/callback"

_JWT = re.compile(r"eyJ[\w-]{4,}\.eyJ[\w-]{4,}\.")


def contains_jwt(text: str) -> bool:
    return _JWT.search(text) is not None


class KeycloakIdP:
    """The settings that point services at the Compose Keycloak."""

    def idp_env(self, service: str) -> dict[str, str]:
        admin_secret = {
            "users-svc": ENV["SF_USERS_ADMIN_CLIENT_SECRET"],
            "authz-svc": ENV["SF_AUTHZ_ADMIN_CLIENT_SECRET"],
        }.get(service, "unused")
        return {
            "SF_OIDC_ISSUER": ISSUER,
            "SF_OIDC_JWKS_URI": f"{ISSUER}/protocol/openid-connect/certs",
            "SF_OIDC_TOKEN_ENDPOINT": f"{ISSUER}/protocol/openid-connect/token",
            "SF_KEYCLOAK_ADMIN_BASE_URL": _ADMIN,
            "SF_KEYCLOAK_ADMIN_CLIENT_SECRET": admin_secret,
            "SF_SVC_AUTHZ_CLIENT_SECRET": ENV["SF_SVC_AUTHZ_CLIENT_SECRET"],
        }


def totp(secret: str, at: float) -> str:
    """RFC 6238, Keycloak's defaults (SHA-1, 6 digits, 30 s). Keycloak's
    HMAC key is the secret's raw bytes, not the base32 form it shows users."""
    mac = hmac.new(secret.encode(), struct.pack(">Q", int(at // 30)), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    code = (struct.unpack(">I", mac[offset : offset + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{code:06d}"


@dataclass
class Authenticator:
    """The user's phone: an enrolled TOTP secret."""

    secret: str
    _used: set[str] = field(default_factory=set)

    def code(self) -> str:
        # Keycloak rejects a code it has already accepted in this window:
        # use this window's code, else the next one (inside Keycloak's
        # look-around), else wait for a fresh window.
        while True:
            for ahead in (0, 30):
                code = totp(self.secret, time.time() + ahead)
                if code not in self._used:
                    self._used.add(code)
                    return code
            time.sleep(2)


@dataclass
class KcUser:
    id: str
    username: str
    password: str
    authenticator: Authenticator | None = None


class KeycloakAdmin:
    """Master-realm admin, only for test setup and for reading results back."""

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def _headers(self) -> dict[str, str]:
        resp = await self._http.post(
            f"{KC}/realms/master/protocol/openid-connect/token",
            data={
                "client_id": "admin-cli",
                "grant_type": "password",
                "username": "admin",
                "password": ENV["KEYCLOAK_ADMIN_PASSWORD"],
            },
        )
        resp.raise_for_status()
        return {"authorization": f"Bearer {resp.json()['access_token']}"}

    async def create_user(self, *, groups: tuple[str, ...] = ()) -> KcUser:
        username = f"systest-{uuid.uuid4().hex[:10]}"
        password = f"Pw-{uuid.uuid4().hex}"
        resp = await self._http.post(
            f"{_ADMIN}/users",
            headers=await self._headers(),
            json={
                "username": username,
                "email": f"{username}@example.com",
                "emailVerified": True,
                "firstName": "System",
                "lastName": "Test",
                "enabled": True,
                "credentials": [{"type": "password", "value": password, "temporary": False}],
                "groups": list(groups),
            },
        )
        resp.raise_for_status()
        return KcUser(resp.headers["location"].rsplit("/", 1)[-1], username, password)

    async def delete_user(self, user: KcUser) -> None:
        await self._http.delete(f"{_ADMIN}/users/{user.id}", headers=await self._headers())

    async def get_user(self, user: KcUser) -> dict:
        resp = await self._http.get(f"{_ADMIN}/users/{user.id}", headers=await self._headers())
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]

    async def sessions(self, user: KcUser) -> list[dict]:
        resp = await self._http.get(
            f"{_ADMIN}/users/{user.id}/sessions", headers=await self._headers()
        )
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]


@dataclass(frozen=True)
class SignIn:
    #: Keycloak pages shown, in order: "password", "otp", "enroll-otp".
    pages: list[str]
    #: Where the BFF sent the browser at the end, or None if Keycloak kept
    #: the browser on its own page (e.g. a refused login).
    location: str | None
    last_page_text: str = ""


_PAGES = {
    "kc-form-login": "password",
    "kc-otp-login-form": "otp",
    "kc-totp-settings-form": "enroll-otp",
}


class Browser:
    """One browser: a cookie jar per host, in front of the BFF replicas.

    Cookies are forwarded by hand: Keycloak marks its cookies Secure even on
    http://localhost, and httpx's jar (rightly) won't send those over http.
    ``bff_responses`` keeps every response the BFF sent this browser.
    """

    def __init__(
        self, http: httpx.AsyncClient, replicas: list[str], *, client_ip: str | None = None
    ) -> None:
        self._http = http
        self._replicas = replicas
        self._next_replica = 0
        self._jars: dict[str, dict[str, str]] = {}
        self.bff_responses: list[httpx.Response] = []
        # Every Browser otherwise shares one real source IP (127.0.0.1, the
        # test process itself), landing every browser in this suite in the
        # BFF's own "auth" rate-limit bucket together (LLD-09 §7.2, keyed by
        # IP) -- a real multi-user deployment never sees this many logins
        # from one IP, but this harness's whole point is driving many in a
        # row. A distinct synthetic IP per ``Browser`` is a closer
        # simulation of distinct real users, not just a workaround.
        self._client_ip = client_ip

    def cookies(self, url: str) -> dict[str, str]:
        return self._jars.setdefault(urlsplit(url).hostname or "", {})

    async def request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        jar = self.cookies(url)
        headers = dict(kwargs.pop("headers", None) or {})  # type: ignore[call-overload]
        if jar:
            headers["cookie"] = "; ".join(f"{k}={v}" for k, v in jar.items())
        if self._client_ip is not None:
            headers["x-forwarded-for"] = self._client_ip
        resp = await self._http.request(method, url, headers=headers, **kwargs)  # type: ignore[arg-type]
        for set_cookie in resp.headers.get_list("set-cookie"):
            name, _, rest = set_cookie.partition("=")
            value, _, attrs = rest.partition(";")
            if "max-age=0" in attrs.lower().replace(" ", ""):
                jar.pop(name.strip(), None)
            else:
                jar[name.strip()] = value
        return resp

    async def bff(
        self, method: str, path: str, *, replica: int | None = None, **kwargs: object
    ) -> httpx.Response:
        """A request to the app. Replicas alternate unless one is named."""
        if replica is None:
            replica = self._next_replica
            self._next_replica = (self._next_replica + 1) % len(self._replicas)
        resp = await self.request(method, self._replicas[replica] + path, **kwargs)
        self.bff_responses.append(resp)
        return resp

    async def sign_in(self, user: KcUser, path: str = "/auth/login") -> SignIn:
        """Follows the login from the BFF through Keycloak and back."""
        resp = await self.bff("GET", path)
        pages: list[str] = []
        for _ in range(20):
            if resp.is_redirect:
                location = resp.headers["location"]
                if location.startswith(CALLBACK):
                    resp = await self.bff("GET", location.removeprefix(APP_ORIGIN))
                elif location.startswith(KC):
                    resp = await self.request("GET", location)
                else:
                    return SignIn(pages, location)
                continue
            form = re.search(r'<form[^>]*\bid="([^"]+)"', resp.text)
            page = _PAGES.get(form.group(1), form.group(1)) if form else f"HTTP {resp.status_code}"
            if page not in _PAGES.values() or (page == "password" and "password" in pages):
                # Not a page we know how to fill, or Keycloak refused the
                # password and asked again.
                return SignIn(pages, None, _visible_text(resp.text))
            if pages and page == pages[-1]:
                raise AssertionError(f"Keycloak refused {page!r}: {_error_text(resp.text)}")
            pages.append(page)
            resp = await self.request(
                "POST", _form_action(resp.text), data=_fill(page, resp.text, user)
            )
        raise AssertionError(f"login did not finish; pages so far: {pages}")


def _form_action(page: str) -> str:
    match = re.search(r'<form[^>]*\baction="([^"]+)"', page)
    assert match, "page has no form action"
    return html.unescape(match.group(1))


def _fill(page: str, text: str, user: KcUser) -> dict[str, str]:
    if page == "password":
        return {"username": user.username, "password": user.password}
    if page == "otp":
        assert user.authenticator, "Keycloak asked for an OTP the user never enrolled"
        return {"otp": user.authenticator.code()}
    # enroll-otp: read the secret Keycloak offers, as the user's phone would
    # from the QR code, and answer with a code from it.
    inputs = [
        dict(re.findall(r'([\w-]+)="([^"]*)"', tag)) for tag in re.findall(r"<input([^>]*)>", text)
    ]
    hidden = {
        i["name"]: html.unescape(i.get("value", "")) for i in inputs if i.get("type") == "hidden"
    }
    user.authenticator = Authenticator(hidden["totpSecret"])
    return {**hidden, "totp": user.authenticator.code(), "userLabel": "systest"}


def _visible_text(page: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", page))


def _error_text(page: str) -> str:
    errors = re.findall(r'id="input-error[^"]*"[^>]*>(.*?)</span>', page, re.S)
    errors += re.findall(r'class="[^"]*alert-error[^"]*".*?<span[^>]*>(.*?)</span>', page, re.S)
    return " | ".join(" ".join(_visible_text(e).split()) for e in errors) or "(no message)"
