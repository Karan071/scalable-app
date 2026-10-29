"""inventory-svc entry point (LLD-07)."""

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
    build_dlq_router,
)
from sqlalchemy import text

from . import event_handlers
from .db import build_inventory_engine, build_inventory_session_factory
from .routes import require_dlq_scope, router
from .settings import Settings
from .sweeper import ExpirySweeper

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


# LLD-08 §7.1 DLQ inspect/replay/discard (E4.5): its own tiny pool (LLD-09
# §2's bulkhead pattern -- an ops action can't contend with request
# handling or the relay/consumers for a connection), built at module scope
# because the router it backs must exist before `assert_routes_covered`
# runs below, which is before `lifespan` ever starts.
_dlq_engine = build_inventory_engine(settings, **worker_pool(2))


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    engine = build_inventory_engine(settings)
    # LLD-09 §2 bulkheads: requests, the relay, each consumer, and the
    # sweeper all get their own pool, so a backlog in one can't starve
    # the others.
    relay_engine = build_inventory_engine(settings, **worker_pool(RELAY_POOL_SIZE))
    catalog_consumer_engine = build_inventory_engine(
        settings, **worker_pool(DEFAULT_HANDLER_CONCURRENCY)
    )
    commands_consumer_engine = build_inventory_engine(
        settings, **worker_pool(DEFAULT_HANDLER_CONCURRENCY)
    )
    sweeper_engine = build_inventory_engine(settings, **worker_pool(2))

    session_factory = build_inventory_session_factory(engine)
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

    relay = OutboxRelay(
        service_name="inventory-svc",
        session_factory=build_inventory_session_factory(relay_engine),
        dsn_asyncpg=_asyncpg_dsn(settings),
        amqp_url=_amqp_url(settings),
        notify_channel="outbox_inventory",
    )
    await relay.start()
    app.state.relay = relay

    catalog_consumer = InboxConsumer(
        consumer_name=event_handlers.CONSUMER_NAME_CATALOG,
        session_factory=build_inventory_session_factory(catalog_consumer_engine),
        amqp_url=_amqp_url(settings),
        queue_name="q.inventory.catalog",
        handlers=event_handlers.CATALOG_DISPATCH,
        # LLD-08 §7 retry tiers (E4.5): a concurrent zero-stock-row insert
        # can deadlock under load, same class of failure sf_db's own
        # retry_on_transient already retries locally, just past that
        # budget. Needs q.inventory.catalog.retry.1-4 to exist
        # (deploy/compose/rabbitmq/topology.sh's queue_with_dlq_and_retries).
        enable_retry_tiers=True,
    )
    await catalog_consumer.start()
    app.state.catalog_consumer = catalog_consumer

    commands_session_factory = build_inventory_session_factory(commands_consumer_engine)
    commands_consumer = InboxConsumer(
        consumer_name=event_handlers.CONSUMER_NAME_COMMANDS,
        session_factory=commands_session_factory,
        amqp_url=_amqp_url(settings),
        queue_name="q.inventory.commands",
        handlers=event_handlers.build_command_dispatch(session_factory),
    )
    await commands_consumer.start()
    app.state.commands_consumer = commands_consumer

    sweeper = ExpirySweeper(
        session_factory=build_inventory_session_factory(sweeper_engine),
        interval_s=settings.sweeper_interval_seconds,
        batch_size=settings.sweeper_batch_size,
    )
    await sweeper.start()
    app.state.sweeper = sweeper

    yield

    await sweeper.stop()
    await commands_consumer.stop()
    await catalog_consumer.stop()
    await relay.stop()
    await watcher.stop()
    await redis.aclose()
    await engine.dispose()
    await relay_engine.dispose()
    await catalog_consumer_engine.dispose()
    await commands_consumer_engine.dispose()
    await sweeper_engine.dispose()
    await _dlq_engine.dispose()


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


async def _background_workers_ready() -> bool:
    try:
        return (
            app.state.relay.is_running
            and app.state.catalog_consumer.is_running
            and app.state.commands_consumer.is_running
            and app.state.sweeper.is_running
        )
    except Exception:
        return False


app = FastAPI(title=settings.service_name, lifespan=lifespan)
app.add_middleware(CorrelationIdMiddleware)
install_problem_details(app, db_busy_errors=POOL_TIMEOUT_ERRORS)
app.include_router(
    build_health_router(readiness_checks=[_db_ready, _authz_ready, _background_workers_ready])
)
app.include_router(router)
app.include_router(
    build_dlq_router(
        service_name="inventory-svc",
        queue_names=["q.inventory.catalog", "q.inventory.commands"],
        amqp_url=_amqp_url(settings),
        session_factory=build_inventory_session_factory(_dlq_engine),
        notify_channel="outbox_inventory",
        auth=require_dlq_scope,
        principal_dependency=require_dlq_scope,
    )
)
assert_routes_covered(app)
instrument_fastapi_app(app)
