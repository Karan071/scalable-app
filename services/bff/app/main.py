"""bff entry point (LLD-01 §2)."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from redis.asyncio import Redis
from sf_common import (
    CorrelationIdMiddleware,
    TokenCipher,
    build_health_router,
    configure_logging,
    init_telemetry,
    install_problem_details,
    instrument_fastapi_app,
)

from .oidc import OidcClient, discover
from .proxy import ConcurrencyGate
from .proxy import router as api_router
from .routes_auth import router as auth_router
from .sessions import SessionStore
from .settings import Settings
from .users_client import ServiceTokenSource, UsersClient

settings = Settings()
configure_logging(settings)
init_telemetry(settings)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    http_client = httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=2.0))
    redis: Redis = Redis.from_url(settings.redis_session_url, decode_responses=True)

    discovery = await discover(http_client, settings.oidc_issuer)
    oidc = OidcClient(
        http_client=http_client,
        discovery=discovery,
        client_id=settings.oidc_client_id,
        client_secret=settings.oidc_client_secret,
        redirect_uri=settings.oidc_redirect_uri,
        clock_leeway_s=settings.clock_leeway_s,
    )
    cipher = TokenCipher.from_base64_keys(
        settings.session_enc_keys, settings.session_enc_primary_kid
    )
    token_source = ServiceTokenSource(
        http_client,
        token_endpoint=discovery.token_endpoint,
        client_id=settings.svc_bff_client_id,
        client_secret=settings.svc_bff_client_secret,
    )

    app.state.settings = settings
    app.state.http_client = http_client
    app.state.redis = redis
    app.state.oidc = oidc
    app.state.sessions = SessionStore(redis, cipher)
    app.state.users_client = UsersClient(
        http_client, base_url=settings.users_svc_base_url, token_source=token_source
    )
    app.state.api_proxy_gate = ConcurrencyGate(settings.api_proxy_max_concurrency)

    yield

    await http_client.aclose()
    await redis.aclose()


async def _redis_ready() -> bool:
    try:
        await app.state.redis.ping()
        return True
    except Exception:
        return False


app = FastAPI(title=settings.service_name, lifespan=lifespan)
app.add_middleware(CorrelationIdMiddleware)
install_problem_details(app)
app.include_router(build_health_router(readiness_checks=[_redis_ready]))
app.include_router(auth_router)
app.include_router(api_router)
instrument_fastapi_app(app)
