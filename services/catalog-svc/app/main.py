"""catalog-svc entry point (LLD-05)."""

import urllib.parse
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from redis.asyncio import Redis
from sf_authz import (
    PolicyWatcher,
    TokenValidator,
    assert_routes_covered,
    build_enforcer,
    install_enforcer,
    install_token_validator,
)
from sf_common import (
    CacheClient,
    CorrelationIdMiddleware,
    build_health_router,
    configure_logging,
    init_telemetry,
    install_problem_details,
    instrument_fastapi_app,
)
from sf_db import POOL_TIMEOUT_ERRORS, worker_pool
from sf_messaging import (
    DEFAULT_HANDLER_CONCURRENCY,
    RELAY_POOL_SIZE,
    InboxConsumer,
    OutboxRelay,
)
from sqlalchemy import text

from . import event_handlers
from .db import build_catalog_engine, build_catalog_session_factory
from .routes import router
from .settings import Settings

settings = Settings()
configure_logging(settings)
init_telemetry(settings)


def _amqp_url(settings: Settings) -> str:
    vhost = urllib.parse.quote(settings.rabbitmq_vhost, safe="")
    return (
        f"amqp://{settings.rabbitmq_user}:{settings.rabbitmq_password}"
        f"@{settings.rabbitmq_host}:{settings.rabbitmq_port}/{vhost}"
    )


def _asyncpg_dsn(settings: Settings) -> str:
    return (
        f"postgresql://{settings.db_user}:{settings.db_password}"
        f"@{settings.db_host}:{settings.db_port}/{settings.db_name}"
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    engine = build_catalog_engine(settings)
    # LLD-09 §2 bulkheads: requests, the relay, and the consumer get their
    # own pool, so a backlog in one can't starve the others.
    relay_engine = build_catalog_engine(settings, **worker_pool(RELAY_POOL_SIZE))
    orgs_projection_engine = build_catalog_engine(
        settings, **worker_pool(DEFAULT_HANDLER_CONCURRENCY)
    )
    session_factory = build_catalog_session_factory(engine)
    app.state.settings = settings
    app.state.session_factory = session_factory
    install_token_validator(
        app, TokenValidator(issuer=settings.oidc_issuer, jwks_uri=settings.oidc_jwks_uri)
    )

    enforcer = build_enforcer(
        db_host=settings.db_host,
        db_port=settings.db_port,
        db_name=settings.db_name,
        db_user=settings.authz_reader_db_user,
        db_password=settings.authz_reader_db_password,
    )
    enforcer.load()
    install_enforcer(app, enforcer)
    redis = Redis.from_url(settings.redis_cache_url, decode_responses=True)
    watcher = PolicyWatcher(enforcer=enforcer, redis_client=redis)
    await watcher.start()
    # LLD-09 §6.1: product detail (storefront), 300s TTL. Same redis-cache
    # connection the policy watcher already holds -- a different key
    # namespace (`cat:v1:prod:*` vs. the watcher's own pub/sub channel),
    # so there's no collision in sharing it.
    app.state.product_cache = CacheClient(redis, name="catalog_product", load_concurrency=20)

    relay = OutboxRelay(
        service_name="catalog-svc",
        session_factory=build_catalog_session_factory(relay_engine),
        dsn_asyncpg=_asyncpg_dsn(settings),
        amqp_url=_amqp_url(settings),
        notify_channel="outbox_catalog",
    )
    await relay.start()
    app.state.relay = relay

    consumer = InboxConsumer(
        consumer_name=event_handlers.CONSUMER_NAME,
        session_factory=build_catalog_session_factory(orgs_projection_engine),
        amqp_url=_amqp_url(settings),
        queue_name="q.catalog.orgs",
        handlers=event_handlers.DISPATCH,
    )
    await consumer.start()
    app.state.consumer = consumer

    yield

    await consumer.stop()
    await relay.stop()
    await watcher.stop()
    await redis.aclose()
    await engine.dispose()
    await relay_engine.dispose()
    await orgs_projection_engine.dispose()


async def _db_ready() -> bool:
    try:
        async with app.state.session_factory() as session:
            await session.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


async def _authz_ready() -> bool:
    try:
        return app.state.authz_enforcer.staleness_s() < 300.0
    except Exception:
        return False


async def _relay_and_consumer_ready() -> bool:
    try:
        return app.state.relay.is_running and app.state.consumer.is_running
    except Exception:
        return False


app = FastAPI(title=settings.service_name, lifespan=lifespan)
app.add_middleware(CorrelationIdMiddleware)
install_problem_details(app, db_busy_errors=POOL_TIMEOUT_ERRORS)
app.include_router(
    build_health_router(readiness_checks=[_db_ready, _authz_ready, _relay_and_consumer_ready])
)
app.include_router(router)
assert_routes_covered(app)
instrument_fastapi_app(app)
