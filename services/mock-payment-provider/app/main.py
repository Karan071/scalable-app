"""mock-payment-provider entry point (ADR-0008, LLD-06 §8)."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from sf_common import (
    CorrelationIdMiddleware,
    build_health_router,
    configure_logging,
    init_telemetry,
    instrument_fastapi_app,
)

from .routes import router
from .settings import Settings
from .store import Store
from .webhooks import WebhookSender

settings = Settings()
configure_logging(settings)
init_telemetry(settings)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.settings = settings
    app.state.store = Store()
    http_client = httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=2.0))
    app.state.http_client = http_client
    sender = WebhookSender(
        store=app.state.store,
        http_client=http_client,
        target_url=settings.webhook_target_url,
        secret=settings.webhook_secret,
    )
    await sender.start()
    app.state.webhook_sender = sender

    yield

    await sender.stop()
    await http_client.aclose()


async def _sender_ready() -> bool:
    try:
        return app.state.webhook_sender.is_running
    except Exception:
        return False


app = FastAPI(title=settings.service_name, lifespan=lifespan)
app.add_middleware(CorrelationIdMiddleware)
# Deliberately not sf_common.install_problem_details: this service mocks
# an *external* PSP's own API, which shapes its own errors
# (`{"error": {"code": ...}}`, ADR-0008) -- ScaleForge's internal
# RFC 9457 convention (ADR-0015) doesn't apply to it. FastAPI's default
# HTTPException handler (`{"detail": ...}`) is what routes.py's
# `detail={"error": {...}}` bodies rely on.
app.include_router(build_health_router(readiness_checks=[_sender_ready]))
app.include_router(router)
instrument_fastapi_app(app)
