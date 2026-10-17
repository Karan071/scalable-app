"""Liveness/readiness endpoints (LLD-12 §7). Liveness never checks dependencies."""

from collections.abc import Awaitable, Callable, Sequence

from fastapi import APIRouter, Response

ReadinessCheck = Callable[[], Awaitable[bool]]


def build_health_router(readiness_checks: Sequence[ReadinessCheck] = ()) -> APIRouter:
    router = APIRouter(tags=["health"])

    @router.get("/livez")
    async def livez() -> dict[str, str]:
        return {"status": "ok"}

    @router.get("/readyz")
    async def readyz(response: Response) -> dict[str, str]:
        for check in readiness_checks:
            if not await check():
                response.status_code = 503
                return {"status": "not_ready"}
        return {"status": "ok"}

    return router
