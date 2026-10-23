"""audit-svc entry point (LLD-04 §2)."""

import urllib.parse
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from redis.asyncio import Redis
from sf_authz import (
    PolicyWatcher,
    TokenValidator,
    assert_routes_covered,
    install_enforcer,
    install_token_validator,
)
from sf_common import (
    CorrelationIdMiddleware,
    build_health_router,
    configure_logging,
    init_telemetry,
    install_problem_details,
    instrument_fastapi_app,
)
from sf_db import worker_pool
from sf_messaging import DEFAULT_HANDLER_CONCURRENCY, InboxConsumer
from sqlalchemy import text

from . import event_handlers
from .db import build_audit_engine, build_audit_reader_enforcer, build_audit_session_factory
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


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    engine = build_audit_engine(settings)
    # LLD-09 §2 bulkheads: requests, the relay, and each consumer get their own
    # pool, so a backlog in one (e.g. slow Keycloak calls in a handler) can't
    # starve the others. Consumer pools match their handler concurrency.
    audit_ingest_engine = build_audit_engine(settings, **worker_pool(DEFAULT_HANDLER_CONCURRENCY))
    redis: Redis = Redis.from_url(settings.redis_cache_url, decode_responses=True)
    enforcer = build_audit_reader_enforcer(settings)
    enforcer.load()

    session_factory = build_audit_session_factory(engine)
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.redis = redis
    install_token_validator(
        app, TokenValidator(issuer=settings.oidc_issuer, jwks_uri=settings.oidc_jwks_uri)
    )
    install_enforcer(app, enforcer)

    watcher = PolicyWatcher(enforcer=enforcer, redis_client=redis)
    await watcher.start()

    consumer = InboxConsumer(
        consumer_name=event_handlers.CONSUMER_NAME,
        session_factory=build_audit_session_factory(audit_ingest_engine),
        amqp_url=_amqp_url(settings),
        queue_name="q.audit.ingest",
        handlers=event_handlers.DISPATCH,
        # audit-svc does not manage policy_version; on_handled is not needed.
    )
    await consumer.start()
    app.state.consumer = consumer

    yield

    await consumer.stop()
    await watcher.stop()
    await redis.aclose()
    await engine.dispose()
    await audit_ingest_engine.dispose()


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


async def _consumer_ready() -> bool:
    try:
        return app.state.consumer.is_running
    except Exception:
        return False


app = FastAPI(title=settings.service_name, lifespan=lifespan)
app.add_middleware(CorrelationIdMiddleware)
install_problem_details(app)
app.include_router(build_health_router(readiness_checks=[_db_ready, _authz_ready, _consumer_ready]))
app.include_router(router)
assert_routes_covered(app)
instrument_fastapi_app(app)
