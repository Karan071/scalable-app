"""BFF auth routes (LLD-01 §4, §7, §8, §9, §10).

Scoped to the login/session/logout/step-up critical path. Deferred (open
items, tracked against LLD-01): back-channel logout (§8, IdP-initiated) and
rate limiting (LLD-09 §7).
"""

import asyncio
import hashlib
import secrets
import time

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from starlette.responses import RedirectResponse

from . import pending_login, proxy
from .csrf import derive_csrf_token, verify_csrf_token
from .oidc import IdTokenInvalid, generate_pkce
from .request_context import check_origin, cookie_names, require_session
from .request_context import state as app_state

router = APIRouter(prefix="/auth")


def _validate_return_to(return_to: str, allowed_prefixes: list[str]) -> str:
    if not return_to.startswith("/") or return_to.startswith("//") or "\\" in return_to:
        return "/"
    if not any(return_to.startswith(p) for p in allowed_prefixes):
        return "/"
    return return_to


async def _start_login(request: Request, *, return_to: str, step_up: bool) -> Response:
    s = app_state(request)
    return_to = _validate_return_to(return_to, s.settings.return_to_allowed_prefixes)

    oidc_state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    pkce = generate_pkce()
    binding = secrets.token_urlsafe(32)
    binding_hash = hashlib.sha256(binding.encode()).hexdigest()

    await pending_login.store(
        s.redis,
        oidc_state,
        {
            "nonce": nonce,
            "verifier": pkce.verifier,
            "return_to": return_to,
            "binding_hash": binding_hash,
            "step_up": step_up,
        },
    )

    authorize_url = s.oidc.build_authorize_url(
        state=oidc_state,
        nonce=nonce,
        code_challenge=pkce.challenge,
        acr_values="2" if step_up else None,
    )

    _session_cookie, binding_cookie_name = cookie_names(request)
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


@router.get("/login")
async def login(request: Request, return_to: str = "/") -> Response:
    return await _start_login(request, return_to=return_to, step_up=False)


@router.get("/step-up")
async def step_up(request: Request, return_to: str = "/") -> Response:
    """LLD-01 §9: ``prompt=login``/``acr_values=2`` re-authentication. The
    binding cookie proves the same browser; the existing session (if any)
    is rotated — not replaced — once the callback completes (see ``callback``
    below), per §9 step 5's "rotates the session ID"."""
    _, _ = await require_session(request)  # must already have a session to step up
    return await _start_login(request, return_to=return_to, step_up=True)


@router.get("/callback")
async def callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> Response:
    s = app_state(request)
    if error or not code or not state:
        return RedirectResponse("/auth/error?code=idp_error", status_code=302)

    pending = await pending_login.consume(s.redis, state)
    if pending is None:
        return RedirectResponse("/auth/error?code=state_invalid", status_code=302)

    session_cookie_name, binding_cookie_name = cookie_names(request)
    binding_cookie = request.cookies.get(binding_cookie_name)
    if (
        binding_cookie is None
        or hashlib.sha256(binding_cookie.encode()).hexdigest() != pending["binding_hash"]
    ):
        return RedirectResponse("/auth/error?code=binding_invalid", status_code=302)

    is_step_up = bool(pending.get("step_up"))
    old_session_id: str | None = None
    if is_step_up:
        # The binding cookie proved the same browser; the pre-step-up
        # session itself must still be present and valid to rotate from.
        old_session_id = request.cookies.get(session_cookie_name)
        if old_session_id is None:
            return RedirectResponse("/auth/error?code=state_invalid", status_code=302)
        old_record = await s.sessions.load(
            old_session_id, idle_s=s.settings.session_idle_standard_s
        )
        if old_record is None:
            return RedirectResponse("/auth/error?code=state_invalid", status_code=302)

    try:
        tokens = await s.oidc.exchange_code(code=code, code_verifier=pending["verifier"])
        principal = s.oidc.validate_id_token(str(tokens["id_token"]), nonce=pending["nonce"])
    except (IdTokenInvalid, httpx.HTTPStatusError):
        return RedirectResponse("/auth/error?code=idp_error", status_code=302)

    if is_step_up:
        acr = str(principal.claim("acr") or "1")
        if int(acr) < 2:
            # The IdP didn't actually step up (e.g. cancelled MFA prompt) —
            # don't silently accept a session rotation with no acr gain.
            return RedirectResponse("/auth/error?code=step_up_not_satisfied", status_code=302)
        uid, sub, idp_sid = old_record.uid, old_record.sub, old_record.idp_sid
    else:
        provision = await s.users_client.provision(
            idp_issuer=str(principal.claims["iss"]),
            idp_subject=principal.subject,
            email=str(principal.claim("email")),
            email_verified=bool(principal.claim("email_verified")),
        )
        if provision.status != "active":
            return RedirectResponse("/account-disabled", status_code=302)

        if provision.needs_claim_refresh:
            # First login: the id/access tokens predate users-svc setting
            # `sf_uid` on the Keycloak user, so refresh to pick it up (§4).
            tokens = await s.oidc.refresh(str(tokens["refresh_token"]))
            principal = s.oidc.validate_id_token(str(tokens["id_token"]), nonce=None)
        uid = provision.user_id
        sub = principal.subject
        idp_sid = str(principal.claim("sid") or "")

    # The CSRF token is derived from `record.csrf` on demand by GET /auth/session,
    # not minted or returned here.
    session_id, _csrf_secret = await s.sessions.create(
        uid=uid,
        sub=sub,
        idp_sid=idp_sid,
        access_token=str(tokens["access_token"]),
        refresh_token=str(tokens["refresh_token"]),
        id_token=str(tokens["id_token"]),
        at_exp=int(time.time()) + int(tokens["expires_in"]),
        acr=str(principal.claim("acr") or "1"),
        auth_time=int(principal.claim("auth_time") or time.time()),
        # Every session starts "standard" on a fresh login; step-up itself
        # doesn't change session_class (that's a separate privileged-session
        # concept, not reopened here — distinguishing it still needs a role
        # lookup done elsewhere, same open item as before E2.10).
        session_class="standard" if not is_step_up else old_record.session_class,
        idle_s=s.settings.session_idle_standard_s,
        abs_s=s.settings.session_abs_standard_s,
    )

    if old_session_id is not None:
        # Rotate, not replace: delete the pre-step-up session only after the
        # new one is durably written (LLD-01 §9 step 5).
        await s.sessions.delete(old_session_id)

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


async def _session_hints(request: Request, access_token: str) -> tuple[list[dict], list[str]]:
    """``memberships[{org_id,name,roles[]}]``/``platform_roles[]`` (LLD-01
    §10). Both are UI hints only ("Roles are for UI only" — the LLD's own
    words); either downstream call failing degrades to an empty list rather
    than failing the whole bootstrap."""
    memberships_result, assignments_result = await asyncio.gather(
        proxy.forward(
            request,
            service="orgs",
            downstream_path="v1/me/memberships",
            access_token=access_token,
            method="GET",
        ),
        proxy.forward(
            request,
            service="authz",
            downstream_path="v1/me/role-assignments",
            access_token=access_token,
            method="GET",
        ),
        return_exceptions=True,
    )

    memberships_raw: list[dict] = []
    if (
        isinstance(memberships_result, proxy.ForwardResult)
        and memberships_result.status_code == 200
    ):
        parsed = proxy.body_as_content(memberships_result.body)
        if isinstance(parsed, list):
            memberships_raw = parsed

    assignments: list[dict] = []
    if (
        isinstance(assignments_result, proxy.ForwardResult)
        and assignments_result.status_code == 200
    ):
        parsed = proxy.body_as_content(assignments_result.body)
        if isinstance(parsed, list):
            assignments = parsed

    platform_roles = [a["role_id"] for a in assignments if a.get("domain") == "platform"]
    memberships = [
        {
            "org_id": m["org_id"],
            "name": m.get("org_name"),
            "roles": [a["role_id"] for a in assignments if a.get("domain") == f"org:{m['org_id']}"],
        }
        for m in memberships_raw
        if m.get("status") == "active"
    ]
    return memberships, platform_roles


@router.get("/session")
async def session_info(request: Request) -> dict[str, object]:
    s = app_state(request)
    session_cookie, _ = cookie_names(request)
    session_id = request.cookies.get(session_cookie)
    if session_id is None:
        return {"authenticated": False}

    record = await s.sessions.load(session_id, idle_s=s.settings.session_idle_standard_s)
    if record is None:
        return {"authenticated": False}

    try:
        access_token = await proxy.ensure_fresh_access_token(s, session_id, record)
    except HTTPException as exc:
        if exc.status_code == 401:
            return {"authenticated": False}
        raise

    me = await s.users_client.get_me(access_token)
    memberships, platform_roles = await _session_hints(request, access_token)
    return {
        "authenticated": True,
        "user": {"id": me["id"], "handle": me["handle"], "display_name": me["display_name"]},
        "csrf_token": derive_csrf_token(record.csrf),
        "memberships": memberships,
        "platform_roles": platform_roles,
        "acr": record.acr,
        "session_class": record.session_class,
    }


@router.post("/logout")
async def logout(request: Request) -> Response:
    check_origin(request)
    s = app_state(request)
    session_id, record = await require_session(request)

    if not verify_csrf_token(record.csrf, request.headers.get("x-csrf-token", "")):
        raise HTTPException(status_code=403, detail="csrf")

    await s.sessions.delete(session_id)

    logout_url = s.oidc.end_session_url(
        id_token=record.id_token, post_logout_redirect_uri=s.settings.app_origin
    )
    session_cookie, _ = cookie_names(request)
    response = RedirectResponse(logout_url, status_code=303)
    response.delete_cookie(session_cookie, path="/")
    return response


@router.post("/logout-all")
async def logout_all(request: Request) -> dict[str, object]:
    check_origin(request)
    s = app_state(request)
    _session_id, record = await require_session(request)

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
