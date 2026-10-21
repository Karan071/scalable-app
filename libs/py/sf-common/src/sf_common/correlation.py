"""Correlation ID: accepted from the client if well-formed, else generated
(LLD-01 §6 step 2, LLD-10 §4). Distinct from the OTel trace ID -- it's a
request-scoped identifier the client can quote back for support, carried
across service hops via the ``X-Correlation-Id`` header, independent of
whether tracing is sampled.
"""

import re
import uuid
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)


def get_correlation_id() -> str | None:
    """Reads the current request's correlation ID, if any (for logs and
    outbound-request headers). ``None`` outside a request context."""
    return _correlation_id.get()


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        raw = request.headers.get("x-correlation-id")
        correlation_id = raw if raw and _UUID_RE.match(raw) else str(uuid.uuid4())
        token = _correlation_id.set(correlation_id)
        try:
            response = await call_next(request)
        finally:
            _correlation_id.reset(token)
        response.headers["x-correlation-id"] = correlation_id
        return response
