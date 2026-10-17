"""BFF auth routes (LLD-01 §4, §7, §8, §10).

Scoped to the login/session/logout critical path. Deferred (open items,
tracked against LLD-01): step-up (§9), back-channel logout (§8, IdP-initiated),
the token-refresh distributed lock (§6 — no `/api/*` proxy exists yet to need
it), and rate limiting (LLD-09 §7).
"""

import hashlib
import secrets
import time

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from starlette.responses import RedirectResponse

from . import pending_login
from .csrf import derive_csrf_token, verify_csrf_token
from .oidc import IdTokenInvalid, generate_pkce

router = APIRouter(prefix="/auth")


def _state(request: Request):  # noqa: ANN202 - app.state is untyped by FastAPI
    return request.app.state


def _cookie_names(request: Request) -> tuple[str, str]:
    """``__Host-`` requires Secure (browser-enforced), so it's only used when Secure is."""
    if _state(request).settings.cookie_secure:
        return "__Host-sf_session", "__Host-sf_auth"
    return "sf_session", "sf_auth"


def _validate_return_to(return_to: str, allowed_prefixes: list[str]) -> str:
    if not return_to.startswith("/") or return_to.startswith("//") or "\\" in return_to:
        return "/"
    if not any(return_to.startswith(p) for p in allowed_prefixes):
        return "/"
    return return_to


def _check_origin(request: Request) -> None:
    app_origin = _state(request).settings.app_origin
    origin = request.headers.get("origin") or request.headers.get("referer")
    if origin is not None and not origin.rstrip("/").startswith(app_origin.rstrip("/")):
        raise HTTPException(status_code=403, detail="origin mismatch")


async def _require_session(request: Request):  # noqa: ANN202
    s = _state(request)
    session_cookie, _ = _cookie_names(request)
    session_id = request.cookies.get(session_cookie)
    if session_id is None:
        raise HTTPException(status_code=401, detail="no session")
    record = await s.sessions.load(session_id, idle_s=s.settings.session_idle_standard_s)
    if record is None:
        raise HTTPException(status_code=401, detail="session_expired")
    return session_id, record


@router.get("/login")
async def login(request: Request, return_to: str = "/") -> Response:
    s = _state(request)
    return_to = _validate_return_to(return_to, s.settings.return_to_allowed_prefixes)

    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    pkce = generate_pkce()
    binding = secrets.token_urlsafe(32)
    binding_hash = hashlib.sha256(binding.encode()).hexdigest()

    await pending_login.store(
        s.redis,
        state,
        {
            "nonce": nonce,
            "verifier": pkce.verifier,
            "return_to": return_to,
            "binding_hash": binding_hash,
        },
    )

    authorize_url = s.oidc.build_authorize_url(
        state=state, nonce=nonce, code_challenge=pkce.challenge
    )

    _session_cookie, binding_cookie_name = _cookie_names(request)
    response = RedirectResponse(authorize_url, status_code=302)
    response.set_cookie(
        binding_cookie_name,
        binding,
        max_age=600,
        httponly=True,
        secure=s.settings.cookie_secure,
        samesite="lax",
        path="/",
    )
    return response


@router.get("/callback")
async def callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> Response:
    s = _state(request)
    if error or not code or not state:
        return RedirectResponse("/auth/error?code=idp_error", status_code=302)

    pending = await pending_login.consume(s.redis, state)
    if pending is None:
        return RedirectResponse("/auth/error?code=state_invalid", status_code=302)

    _session_cookie, binding_cookie_name = _cookie_names(request)
    binding_cookie = request.cookies.get(binding_cookie_name)
    if (
        binding_cookie is None
        or hashlib.sha256(binding_cookie.encode()).hexdigest() != pending["binding_hash"]
    ):
        return RedirectResponse("/auth/error?code=binding_invalid", status_code=302)

    try:
        tokens = await s.oidc.exchange_code(code=code, code_verifier=pending["verifier"])
        principal = s.oidc.validate_id_token(str(tokens["id_token"]), nonce=pending["nonce"])
    except (IdTokenInvalid, httpx.HTTPStatusError):
        return RedirectResponse("/auth/error?code=idp_error", status_code=302)

    provision = await s.users_client.provision(
        idp_issuer=str(principal.claims["iss"]),
        idp_subject=principal.subject,
        email=str(principal.claim("email")),
        email_verified=bool(principal.claim("email_verified")),
    )
    if provision.status != "active":
        return RedirectResponse("/account-disabled", status_code=302)

    if provision.needs_claim_refresh:
        # First login: the id/access tokens predate users-svc setting `sf_uid`
        # on the Keycloak user, so refresh to pick up the claim (LLD-01 §4).
        tokens = await s.oidc.refresh(str(tokens["refresh_token"]))
        principal = s.oidc.validate_id_token(str(tokens["id_token"]), nonce=None)

    # The CSRF token is derived from `record.csrf` on demand by GET /auth/session,
    # not minted or returned here.
    session_id, _csrf_secret = await s.sessions.create(
        uid=provision.user_id,
        sub=principal.subject,
        idp_sid=str(principal.claim("sid") or ""),
        access_token=str(tokens["access_token"]),
        refresh_token=str(tokens["refresh_token"]),
        id_token=str(tokens["id_token"]),
        at_exp=int(time.time()) + int(tokens["expires_in"]),
        acr=str(principal.claim("acr") or "1"),
        auth_time=int(principal.claim("auth_time") or time.time()),
        # Every session starts "standard": distinguishing "privileged" needs a
        # role lookup, which needs authz-svc (E2.7, open item).
        session_class="standard",
        idle_s=s.settings.session_idle_standard_s,
        abs_s=s.settings.session_abs_standard_s,
    )

    session_cookie_name, binding_cookie_name = _cookie_names(request)
    response = RedirectResponse(pending["return_to"], status_code=302)
    response.set_cookie(
        session_cookie_name,
        session_id,
        httponly=True,
        secure=s.settings.cookie_secure,
        samesite="lax",
        path="/",
    )
    response.delete_cookie(binding_cookie_name, path="/")
    return response


@router.get("/session")
async def session_info(request: Request) -> dict[str, object]:
    s = _state(request)
    session_cookie, _ = _cookie_names(request)
    session_id = request.cookies.get(session_cookie)
    if session_id is None:
        return {"authenticated": False}

    record = await s.sessions.load(session_id, idle_s=s.settings.session_idle_standard_s)
    if record is None:
        return {"authenticated": False}

    me = await s.users_client.get_me(record.access_token)
    return {
        "authenticated": True,
        "user": {"id": me["id"], "handle": me["handle"], "display_name": me["display_name"]},
        "csrf_token": derive_csrf_token(record.csrf),
        "acr": record.acr,
        "session_class": record.session_class,
    }


@router.post("/logout")
async def logout(request: Request) -> Response:
    _check_origin(request)
    s = _state(request)
    session_id, record = await _require_session(request)

    if not verify_csrf_token(record.csrf, request.headers.get("x-csrf-token", "")):
        raise HTTPException(status_code=403, detail="csrf")

    await s.sessions.delete(session_id)

    logout_url = s.oidc.end_session_url(
        id_token=record.id_token, post_logout_redirect_uri=s.settings.app_origin
    )
    session_cookie, _ = _cookie_names(request)
    response = RedirectResponse(logout_url, status_code=303)
    response.delete_cookie(session_cookie, path="/")
    return response


@router.post("/logout-all")
async def logout_all(request: Request) -> dict[str, object]:
    _check_origin(request)
    s = _state(request)
    _session_id, record = await _require_session(request)

    if not verify_csrf_token(record.csrf, request.headers.get("x-csrf-token", "")):
        raise HTTPException(status_code=403, detail="csrf")

    revoked = await s.sessions.delete_all_for_user(record.uid)
    return {"revoked": revoked}


@router.get("/error")
async def auth_error(code: str = "unknown") -> dict[str, str]:
    return {"error": code}


@router.get("/account-disabled")
async def account_disabled() -> dict[str, str]:
    return {"status": "account_disabled"}
