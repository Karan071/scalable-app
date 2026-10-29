"""Generic authenticated reverse proxy for ``/api/**`` (LLD-01 §6).

Also used internally by ``routes_auth.py``'s ``GET /auth/session`` to enrich
bootstrap with memberships/role-assignments — same "load session -> refresh
if needed -> forward with the user's own access token" pipeline, so there's
exactly one place that does it.

Step-up detection is reactive rather than a BFF-side route table: downstream
services' own ``Authorize(..., min_acr=...)`` is already the authority on
which routes need acr>=2 (it floors ``effective_min_acr`` to 2 for any
``domain="platform"`` route regardless of what's passed — see
``sf_authz.dependencies``), so a parallel BFF-side table would only risk
drifting out of sync with the real enforcement. Instead this proxy watches
for the downstream's ``401`` + problem ``title == "step_up_required"`` and
rewrites it into the BFF's own step-up-shaped response (LLD-01 §9 step 2).
``step_up_url`` deliberately omits ``return_to``: the BFF has no visibility
into which SPA route is open (it only sees the API path called), so the
SPA's own global error handler appends ``?return_to=<current page>``.
"""

import asyncio
import json
import time
import uuid

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sf_common import get_correlation_id
from starlette.datastructures import State

from .csrf import verify_csrf_token
from .ratelimit import classify as classify_rate_limit
from .ratelimit import enforce as enforce_rate_limit
from .request_context import check_origin, require_session
from .request_context import state as app_state
from .sessions import SessionRecord, key_hash

router = APIRouter(prefix="/api")

_MAX_BODY_BYTES = 1 * 1024 * 1024
_REFRESH_SKEW_S = 60
_LOCK_TTL_MS = 5000
_LOCK_MIN_VALID_S = 5
_LOCK_POLL_INTERVAL_S = 0.1
_LOCK_POLL_TIMEOUT_S = 1.5
_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# Allow-listed request headers the SPA sets that a downstream route
# actually requires (LLD-06 §4's `Idempotency-Key`; `If-Match`/`If-None-
# Match` for every optimistic-locking route this proxy fronts — catalog/
# inventory/orders/authz's own `version`-based routes, apps/web/src/lib/
# api/*.ts). Everything else the client sends is dropped, same as before:
# this isn't a transparent proxy, `forward()` builds its own header set.
_FORWARDED_REQUEST_HEADERS = {"idempotency-key", "if-match", "if-none-match"}
_HOP_BY_HOP_RESPONSE_HEADERS = {
    "connection",
    "keep-alive",
    "transfer-encoding",
    "content-encoding",
    "content-length",
}

_RELEASE_LOCK_LUA = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""


class ConcurrencyGate:
    """Non-blocking "acquire or reject" load-shedding cap (LLD-01 §6 step 1).
    ``asyncio.Semaphore`` has no non-blocking acquire, hence this instead."""

    def __init__(self, max_concurrency: int) -> None:
        self._max = max_concurrency
        self._current = 0
        self._lock = asyncio.Lock()

    async def try_acquire(self) -> bool:
        async with self._lock:
            if self._current >= self._max:
                return False
            self._current += 1
            return True

    async def release(self) -> None:
        async with self._lock:
            self._current -= 1


def _service_base_urls(settings: object) -> dict[str, str]:
    return {
        "orgs": settings.orgs_svc_base_url,  # type: ignore[attr-defined]
        "authz": settings.authz_svc_base_url,  # type: ignore[attr-defined]
        "users": settings.users_svc_base_url,  # type: ignore[attr-defined]
        "catalog": settings.catalog_svc_base_url,  # type: ignore[attr-defined]
        "search": settings.search_svc_base_url,  # type: ignore[attr-defined]
        "cart": settings.cart_svc_base_url,  # type: ignore[attr-defined]
        "inventory": settings.inventory_svc_base_url,  # type: ignore[attr-defined]
        "orders": settings.orders_svc_base_url,  # type: ignore[attr-defined]
        # payments-svc has no entry here -- every one of its routes is
        # `Internal(...)` or the provider webhook, never something the
        # browser's own access token should reach (LLD-15 §2: hiding a
        # route from the proxy isn't a security control either way --
        # `Internal` would 403 a user token on its own -- this is just
        # not exposing a path with no legitimate caller).
    }


def correlation_id() -> str:
    """``CorrelationIdMiddleware`` (installed app-wide in main.py) has
    already validated/generated this by the time any route handler runs;
    the fallback below only matters if that invariant is ever broken."""
    return get_correlation_id() or str(uuid.uuid4())


async def ensure_fresh_access_token(state: State, session_id: str, record: SessionRecord) -> str:
    """LLD-01 §6's refresh algorithm. Returns the token to forward (possibly
    refreshed in place — never rotates the session id, so a concurrent
    request on the same cookie keeps working). Raises ``HTTPException(401,
    "session_expired")`` on ``invalid_grant``, or ``HTTPException(503, ...)``
    if neither a fresh refresh nor the current token can be trusted."""
    now = int(time.time())
    if record.at_exp - now >= _REFRESH_SKEW_S:
        return record.access_token

    redis: Redis = state.redis
    lock_key = f"sess:lock:{key_hash(session_id)}"
    lock_token = uuid.uuid4().hex
    acquired = await redis.set(lock_key, lock_token, nx=True, px=_LOCK_TTL_MS)

    if acquired:
        try:
            tokens = await state.oidc.refresh(record.refresh_token)
        except httpx.HTTPStatusError as exc:
            body: dict[str, object] = {}
            try:
                body = exc.response.json()
            except Exception:
                pass
            await redis.eval(_RELEASE_LOCK_LUA, 1, lock_key, lock_token)
            if body.get("error") == "invalid_grant":
                await state.sessions.delete(session_id)
                raise HTTPException(status_code=401, detail="session_expired") from exc
            if record.at_exp - now >= _LOCK_MIN_VALID_S:
                return record.access_token
            raise HTTPException(status_code=503, detail="dependency_unavailable") from exc
        except httpx.HTTPError as exc:
            await redis.eval(_RELEASE_LOCK_LUA, 1, lock_key, lock_token)
            if record.at_exp - now >= _LOCK_MIN_VALID_S:
                return record.access_token
            raise HTTPException(status_code=503, detail="dependency_unavailable") from exc

        new_access_token = str(tokens["access_token"])
        new_at_exp = int(time.time()) + int(tokens["expires_in"])
        await state.sessions.update_tokens(
            session_id,
            access_token=new_access_token,
            refresh_token=str(tokens["refresh_token"]),
            at_exp=new_at_exp,
        )
        await redis.eval(_RELEASE_LOCK_LUA, 1, lock_key, lock_token)
        return new_access_token

    # Someone else holds the lock: poll for their refresh to land.
    deadline = time.monotonic() + _LOCK_POLL_TIMEOUT_S
    while time.monotonic() < deadline:
        await asyncio.sleep(_LOCK_POLL_INTERVAL_S)
        peeked = await state.sessions.peek_access_token(session_id)
        if peeked is None:
            raise HTTPException(status_code=401, detail="session_expired")
        token, at_exp = peeked
        if at_exp > record.at_exp:
            return token

    if record.at_exp - now >= _LOCK_MIN_VALID_S:
        return record.access_token
    raise HTTPException(status_code=503, detail="dependency_unavailable")


class ForwardResult:
    def __init__(self, *, status_code: int, headers: httpx.Headers, body: bytes) -> None:
        self.status_code = status_code
        self.headers = headers
        self.body = body


async def forward(
    request: Request,
    *,
    service: str,
    downstream_path: str,
    access_token: str,
    method: str | None = None,
    json_body: object = None,
) -> ForwardResult:
    """Builds the downstream URL and forwards one request with the user's
    own access token (not a service-credential token — downstream services'
    ``Authorize(...)``/``AuthenticatedUser()`` are the real authorization
    check against it). No retry here — that's the SPA's job (LLD-15 §4)."""
    s = app_state(request)
    base_urls = _service_base_urls(s.settings)
    if service not in base_urls:
        raise HTTPException(status_code=404, detail="unknown service")

    url = f"{base_urls[service].rstrip('/')}/{downstream_path.lstrip('/')}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "X-Correlation-Id": correlation_id(),
    }
    traceparent = request.headers.get("traceparent")
    if traceparent:
        headers["traceparent"] = traceparent
    for name in _FORWARDED_REQUEST_HEADERS:
        value = request.headers.get(name)
        if value is not None:
            headers[name] = value

    http_client: httpx.AsyncClient = s.http_client
    resp = await http_client.request(
        method or request.method,
        url,
        params=request.url.query,
        headers=headers,
        json=json_body,
    )
    return ForwardResult(status_code=resp.status_code, headers=resp.headers, body=resp.content)


def translate_step_up(result: ForwardResult) -> JSONResponse | None:
    """LLD-01 §9 step 2. Returns ``None`` (pass the original response through
    unchanged) for anything that isn't a step-up signal."""
    if result.status_code != 401:
        return None
    try:
        body = json.loads(result.body)
    except Exception:
        return None
    if body.get("title") != "step_up_required":
        return None
    return JSONResponse(
        status_code=401,
        media_type="application/problem+json",
        content={
            "type": "about:blank",
            "title": "step_up_required",
            "status": 401,
            "step_up_url": "/auth/step-up",
        },
    )


def _response_headers(result: ForwardResult) -> dict[str, str]:
    return {
        k: v for k, v in result.headers.items() if k.lower() not in _HOP_BY_HOP_RESPONSE_HEADERS
    }


def body_as_content(body: bytes) -> object:
    if not body:
        return None
    try:
        return json.loads(body)
    except Exception:
        return body.decode(errors="replace")


@router.api_route(
    "/{service}/{downstream_path:path}", methods=["GET", "POST", "PATCH", "PUT", "DELETE"]
)
async def proxy_request(service: str, downstream_path: str, request: Request) -> JSONResponse:
    s = app_state(request)

    content_length = request.headers.get("content-length")
    if content_length is not None and int(content_length) > _MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="payload too large")

    gate: ConcurrencyGate = s.api_proxy_gate
    if not await gate.try_acquire():
        raise HTTPException(status_code=503, detail="dependency_unavailable")

    try:
        session_id, record = await require_session(request)
        await enforce_rate_limit(
            s.rate_limiter,
            classify_rate_limit(service, downstream_path, request.method),
            f"user:{record.uid}",
        )

        json_body: object = None
        if request.method in _UNSAFE_METHODS:
            check_origin(request)
            if not verify_csrf_token(record.csrf, request.headers.get("x-csrf-token", "")):
                raise HTTPException(status_code=403, detail="csrf-invalid")
            content_type = request.headers.get("content-type", "")
            body_bytes = await request.body()
            if body_bytes:
                if not content_type.startswith("application/json"):
                    raise HTTPException(status_code=415, detail="unsupported media type")
                json_body = json.loads(body_bytes)

        access_token = await ensure_fresh_access_token(s, session_id, record)
        result = await forward(
            request,
            service=service,
            downstream_path=downstream_path,
            access_token=access_token,
            json_body=json_body,
        )

        step_up_response = translate_step_up(result)
        if step_up_response is not None:
            return step_up_response

        return JSONResponse(
            status_code=result.status_code,
            content=body_as_content(result.body),
            headers=_response_headers(result),
            media_type=result.headers.get("content-type", "application/json"),
        )
    finally:
        await gate.release()
