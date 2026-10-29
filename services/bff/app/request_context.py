"""Request-scoped helpers shared by `/auth/*` and `/api/**` (LLD-01 §4-§6).

Factored out of routes_auth.py (E2.10) so the new proxy router doesn't
import another router's private helpers.
"""

from fastapi import HTTPException, Request
from starlette.datastructures import State

from .sessions import SessionRecord


def state(request: Request) -> State:
    return request.app.state  # type: ignore[no-any-return]


def cookie_names(request: Request) -> tuple[str, str]:
    """``__Host-`` requires Secure (browser-enforced), so it's only used when Secure is."""
    if state(request).settings.cookie_secure:
        return "__Host-sf_session", "__Host-sf_auth"
    return "sf_session", "sf_auth"


def check_origin(request: Request) -> None:
    app_origin = state(request).settings.app_origin
    origin = request.headers.get("origin") or request.headers.get("referer")
    if origin is not None and not origin.rstrip("/").startswith(app_origin.rstrip("/")):
        raise HTTPException(status_code=403, detail="origin mismatch")


def client_ip(request: Request, *, trusted_hops: int = 1) -> str:
    """LLD-09 §7.2: "taken from X-Forwarded-For only at the configured
    trusted hop count" -- never from the header's first entry, which any
    client can set to anything. ``trusted_hops`` is the number of proxies
    between the client and this process that each append their own view
    of the previous hop (1: the gateway) -- the real client is the entry
    that many positions from the end."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
        if len(hops) >= trusted_hops:
            return hops[-trusted_hops]
    return request.client.host if request.client else "unknown"


async def require_session(request: Request) -> tuple[str, SessionRecord]:
    s = state(request)
    session_cookie, _ = cookie_names(request)
    session_id = request.cookies.get(session_cookie)
    if session_id is None:
        raise HTTPException(status_code=401, detail="unauthenticated")
    record = await s.sessions.load(session_id, idle_s=s.settings.session_idle_standard_s)
    if record is None:
        raise HTTPException(status_code=401, detail="session_expired")
    return session_id, record
