"""payments-svc entry point (LLD-06 §7-§9)."""

import urllib.parse
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from sf_authz import TokenValidator, assert_routes_covered, install_token_validator
from sf_common import (
    CorrelationIdMiddleware,
    build_health_router,
    configure_logging,
    init_telemetry,
    install_problem_details,
    instrument_fastapi_app,
)
from sf_db import POOL_TIMEOUT_ERRORS, worker_pool
from sf_messaging import DEFAULT_HANDLER_CONCURRENCY, RELAY_POOL_SIZE, InboxConsumer, OutboxRelay
from sqlalchemy import text

from . import event_handlers
from .db import build_payments_engine, build_payments_session_factory
from .provider_client import MockProviderClient
from .reconciliation import UnknownPaymentReconciler
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
    engine = build_payments_engine(settings)
    # LLD-09 §2 bulkheads: requests, the relay, the consumer, and the
    # reconciliation worker all get their own pool.
    relay_engine = build_payments_engine(settings, **worker_pool(RELAY_POOL_SIZE))
    commands_consumer_engine = build_payments_engine(
        settings, **worker_pool(DEFAULT_HANDLER_CONCURRENCY)
    )
    reconciliation_engine = build_payments_engine(settings, **worker_pool(2))

    session_factory = build_payments_session_factory(engine)
    app.state.settings = settings
    app.state.session_factory = session_factory
    install_token_validator(
        app, TokenValidator(issuer=settings.oidc_issuer, jwks_uri=settings.oidc_jwks_uri)
    )

    http_client = httpx.AsyncClient()
    app.state.http_client = http_client
    provider = MockProviderClient(http_client, base_url=settings.provider_base_url)
    app.state.provider = provider

    relay = OutboxRelay(
        service_name="payments-svc",
        session_factory=build_payments_session_factory(relay_engine),
        dsn_asyncpg=_asyncpg_dsn(settings),
        amqp_url=_amqp_url(settings),
        notify_channel="outbox_payments",
    )
    await relay.start()
    app.state.relay = relay

    commands_session_factory = build_payments_session_factory(commands_consumer_engine)
    commands_consumer = InboxConsumer(
        consumer_name=event_handlers.CONSUMER_NAME,
        session_factory=commands_session_factory,
        amqp_url=_amqp_url(settings),
        queue_name="q.payments.commands",
        handlers=event_handlers.build_command_dispatch(commands_session_factory, provider),
    )
    await commands_consumer.start()
    app.state.commands_consumer = commands_consumer

    reconciler = UnknownPaymentReconciler(
        session_factory=build_payments_session_factory(reconciliation_engine),
        provider=provider,
        interval_s=settings.reconciliation_interval_seconds,
        unknown_after_s=settings.reconciliation_unknown_after_seconds,
    )
    await reconciler.start()
    app.state.reconciler = reconciler

    yield

    await reconciler.stop()
    await commands_consumer.stop()
    await relay.stop()
    await http_client.aclose()
    await engine.dispose()
    await relay_engine.dispose()
    await commands_consumer_engine.dispose()
    await reconciliation_engine.dispose()


async def _db_ready() -> bool:
    try:
        async with app.state.session_factory() as session:
            await session.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


async def _background_workers_ready() -> bool:
    try:
        return (
            app.state.relay.is_running
            and app.state.commands_consumer.is_running
            and app.state.reconciler.is_running
        )
    except Exception:
        return False


app = FastAPI(title=settings.service_name, lifespan=lifespan)
app.add_middleware(CorrelationIdMiddleware)
install_problem_details(app, db_busy_errors=POOL_TIMEOUT_ERRORS)
app.include_router(build_health_router(readiness_checks=[_db_ready, _background_workers_ready]))
app.include_router(router)
assert_routes_covered(app)
instrument_fastapi_app(app)
