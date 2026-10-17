"""RFC 9457 problem-details error responses for every service (ADR-0015)."""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

_MEDIA_TYPE = "application/problem+json"


def _problem(status: int, title: str, detail: str | None, instance: str) -> JSONResponse:
    body: dict[str, object] = {
        "type": "about:blank",
        "title": title,
        "status": status,
        "instance": instance,
    }
    if detail is not None:
        body["detail"] = detail
    return JSONResponse(status_code=status, content=body, media_type=_MEDIA_TYPE)


def install_problem_details(app: FastAPI) -> None:
    """Register handlers so every error response is ``application/problem+json``."""

    @app.exception_handler(HTTPException)
    async def _http_exception(request: Request, exc: HTTPException) -> JSONResponse:
        return _problem(exc.status_code, str(exc.detail) or "Error", None, str(request.url.path))

    @app.exception_handler(RequestValidationError)
    async def _validation_exception(request: Request, exc: RequestValidationError) -> JSONResponse:
        return _problem(422, "Validation error", str(exc.errors()), str(request.url.path))

    @app.exception_handler(Exception)
    async def _unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
        return _problem(500, "Internal server error", None, str(request.url.path))
