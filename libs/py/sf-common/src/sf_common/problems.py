"""RFC 9457 problem-details error responses for every service (ADR-0015)."""

import logging
from collections.abc import Sequence

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from opentelemetry import metrics
from starlette.exceptions import HTTPException

from .correlation import get_correlation_id

_MEDIA_TYPE = "application/problem+json"
_logger = logging.getLogger("sf_common.problems")
_db_pool_timeouts = metrics.get_meter("sf_common").create_counter(
    "db_pool_timeouts_total",
    unit="{request}",
    description="Requests refused with 503 db-busy: no pooled DB connection within pool_timeout",
)


def _correlation_id(request: Request) -> str | None:
    return get_correlation_id() or getattr(request.state, "correlation_id", None)


def _problem(
    status: int,
    title: str,
    detail: str | None,
    request: Request,
    *,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body: dict[str, object] = {
        "type": "about:blank",
        "title": title,
        "status": status,
        "instance": str(request.url.path),
    }
    if detail is not None:
        body["detail"] = detail
    response_headers = dict(headers or {})
    # ADR-0015: every problem carries the id the client can quote to support.
    correlation_id = _correlation_id(request)
    if correlation_id is not None:
        body["correlation_id"] = correlation_id
        response_headers["x-correlation-id"] = correlation_id
    return JSONResponse(
        status_code=status, content=body, media_type=_MEDIA_TYPE, headers=response_headers
    )


def install_problem_details(
    app: FastAPI, *, db_busy_errors: Sequence[type[Exception]] = ()
) -> None:
    """Register handlers so every error response is ``application/problem+json``.

    ``db_busy_errors``: exception types meaning "no DB connection available
    in time" (``sf_db.POOL_TIMEOUT_ERRORS``), answered with LLD-09 §2's
    fail-fast ``503 db-busy`` instead of a 500.
    """

    @app.exception_handler(HTTPException)
    async def _http_exception(request: Request, exc: HTTPException) -> JSONResponse:
        return _problem(exc.status_code, str(exc.detail) or "Error", None, request)

    @app.exception_handler(RequestValidationError)
    async def _validation_exception(request: Request, exc: RequestValidationError) -> JSONResponse:
        return _problem(422, "Validation error", str(exc.errors()), request)

    @app.exception_handler(Exception)
    async def _unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
        return _problem(500, "Internal server error", None, request)

    async def _db_busy(request: Request, exc: Exception) -> JSONResponse:
        _db_pool_timeouts.add(1)
        _logger.warning("db_pool_exhausted", extra={"path": str(request.url.path)})
        return _problem(
            503,
            "db-busy",
            "The service is at capacity. Retry shortly.",
            request,
            headers={"retry-after": "1"},
        )

    for exc_type in db_busy_errors:
        app.add_exception_handler(exc_type, _db_busy)
