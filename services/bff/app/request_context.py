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
