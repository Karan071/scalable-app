"""bff entry point (LLD-01 §2)."""

import urllib.parse
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

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

from .event_consumer import IdentityEventConsumer
from .oidc import OidcClient, discover
from .proxy import ConcurrencyGate
from .proxy import router as api_router
from .ratelimit import RateLimiter
from .routes_auth import router as auth_router
from .sessions import SessionStore
from .settings import Settings
from .spa import build_spa_router
from .sse import MAX_STREAMS_PER_USER, PerUserStreamLimiter, SseFanoutConsumer
from .sse import router as sse_router
from .users_client import ServiceTokenSource, UsersClient

settings = Settings()
configure_logging(settings)
init_telemetry(settings)


def _amqp_url(settings: Settings) -> str:
    vhost = urllib.parse.quote(settings.rabbitmq_vhost, safe="")
    return (
        f"amqp://{settings.rabbitmq_user}:{settings.rabbitmq_password}"
        f"@{settings.rabbitmq_host}:{settings.rabbitmq_port}/{vhost}"
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    http_client = httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=2.0))
    redis: Redis = Redis.from_url(settings.redis_session_url, decode_responses=True)
    redis_cache: Redis = Redis.from_url(settings.redis_cache_url, decode_responses=True)

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
    app.state.rate_limiter = RateLimiter(
        redis_cache, replica_count=settings.rate_limit_replica_count
    )
    app.state.redis_cache = redis_cache
    app.state.sse_stream_limiter = PerUserStreamLimiter(MAX_STREAMS_PER_USER)

    identity_consumer = IdentityEventConsumer(
        redis=redis,
        sessions=app.state.sessions,
        amqp_url=_amqp_url(settings),
        queue_name="q.bff.identity",
    )
    await identity_consumer.start()
    app.state.identity_consumer = identity_consumer

    sse_consumer = SseFanoutConsumer(
        redis=redis_cache, amqp_url=_amqp_url(settings), queue_name="q.bff.sse"
    )
    await sse_consumer.start()
    app.state.sse_consumer = sse_consumer

    yield

    await sse_consumer.stop()
    await identity_consumer.stop()
    await http_client.aclose()
    await redis.aclose()
    await redis_cache.aclose()


async def _redis_ready() -> bool:
    try:
        await app.state.redis.ping()
        return True
    except Exception:
        return False


async def _identity_consumer_ready() -> bool:
    try:
        return app.state.identity_consumer.is_running
    except Exception:
        return False


async def _sse_consumer_ready() -> bool:
    try:
        return app.state.sse_consumer.is_running
    except Exception:
        return False


app = FastAPI(title=settings.service_name, lifespan=lifespan)
app.add_middleware(CorrelationIdMiddleware)
install_problem_details(app)
app.include_router(
    build_health_router(
        readiness_checks=[_redis_ready, _identity_consumer_ready, _sse_consumer_ready]
    )
)
app.include_router(auth_router)
# Registered before api_router: /api/events must match this router's own
# route, not fall through to the generic /api/{service}/{path:path} proxy.
app.include_router(sse_router)
app.include_router(api_router)
if settings.web_dist_dir is not None:
    dist_dir = Path(settings.web_dist_dir)
    if dist_dir.is_dir():
        # Registered last: /api/** and /auth/** above always match first.
        app.include_router(build_spa_router(dist_dir))
instrument_fastapi_app(app)
