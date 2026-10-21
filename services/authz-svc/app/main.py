"""authz-svc entry point (LLD-02)."""

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
    create_http_client,
    init_telemetry,
    install_problem_details,
    instrument_fastapi_app,
)
from sf_messaging import InboxConsumer, OutboxRelay
from sqlalchemy import text

from . import event_handlers
from .db import build_authz_engine, build_authz_reader_enforcer, build_authz_session_factory
from .orgs_client import OrgsClient, ServiceTokenSource
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
    engine = build_authz_engine(settings)
    http_client = create_http_client()
    redis: Redis = Redis.from_url(settings.redis_cache_url, decode_responses=True)
    enforcer = build_authz_reader_enforcer(settings)
    enforcer.load()  # synchronous; readiness waits on this below

    session_factory = build_authz_session_factory(engine)
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.redis = redis
    app.state.orgs_client = OrgsClient(
        http_client,
        base_url=settings.orgs_svc_base_url,
        token_source=ServiceTokenSource(
            http_client,
            token_endpoint=settings.oidc_token_endpoint,
            client_id=settings.svc_authz_client_id,
            client_secret=settings.svc_authz_client_secret,
        ),
    )
    install_token_validator(
        app, TokenValidator(issuer=settings.oidc_issuer, jwks_uri=settings.oidc_jwks_uri)
    )
    install_enforcer(app, enforcer)

    watcher = PolicyWatcher(enforcer=enforcer, redis_client=redis)
    await watcher.start()

    relay = OutboxRelay(
        service_name="authz-svc",
        session_factory=session_factory,
        dsn_asyncpg=_asyncpg_dsn(settings),
        amqp_url=_amqp_url(settings),
        notify_channel="outbox_authz",
    )
    await relay.start()
    app.state.relay = relay

    async def _on_handled(new_version: int) -> None:
        try:
            await redis.publish("authz:policy", str(new_version))
        except Exception:  # best-effort; the 10s poll fallback still converges
            pass

    consumer = InboxConsumer(
        consumer_name=event_handlers.CONSUMER_NAME,
        session_factory=session_factory,
        amqp_url=_amqp_url(settings),
        queue_name="q.authz.identity",
        handlers=event_handlers.DISPATCH,
        on_handled=_on_handled,
    )
    await consumer.start()
    app.state.consumer = consumer

    yield

    await consumer.stop()
    await relay.stop()
    await watcher.stop()
    await http_client.aclose()
    await redis.aclose()
    await engine.dispose()


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
install_problem_details(app)
app.include_router(
    build_health_router(readiness_checks=[_db_ready, _authz_ready, _relay_and_consumer_ready])
)
app.include_router(router)
assert_routes_covered(app)
instrument_fastapi_app(app)
