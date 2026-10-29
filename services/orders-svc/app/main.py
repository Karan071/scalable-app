"""orders-svc entry point (LLD-06)."""

import urllib.parse
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
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
    Bulkhead,
    BulkheadFullError,
    CircuitBreaker,
    CircuitOpenError,
    CorrelationIdMiddleware,
    RetryBudget,
    build_health_router,
    configure_logging,
    init_telemetry,
    install_problem_details,
    instrument_fastapi_app,
)
from sf_db import POOL_TIMEOUT_ERRORS, worker_pool
from sf_messaging import DEFAULT_HANDLER_CONCURRENCY, RELAY_POOL_SIZE, InboxConsumer, OutboxRelay
from sqlalchemy import text

from . import event_handlers, service
from .clients import CartClient, CatalogClient, InventoryClient, PaymentsClient, ServiceTokenSource
from .db import build_orders_engine, build_orders_session_factory
from .recovery import RecoveryWorker
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
    engine = build_orders_engine(settings)
    # LLD-09 §2 bulkheads: requests, the relay, each consumer, and the
    # recovery worker all get their own pool.
    relay_engine = build_orders_engine(settings, **worker_pool(RELAY_POOL_SIZE))
    inventory_consumer_engine = build_orders_engine(
        settings, **worker_pool(DEFAULT_HANDLER_CONCURRENCY)
    )
    payments_consumer_engine = build_orders_engine(
        settings, **worker_pool(DEFAULT_HANDLER_CONCURRENCY)
    )
    recovery_engine = build_orders_engine(settings, **worker_pool(5))

    session_factory = build_orders_session_factory(engine)
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

    http_client = httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=2.0))
    app.state.http_client = http_client
    token_source = ServiceTokenSource(
        http_client,
        token_endpoint=settings.oidc_token_endpoint,
        client_id=settings.svc_orders_client_id,
        client_secret=settings.svc_orders_client_secret,
    )
    # LLD-13 §2.3-§2.4: one breaker/bulkhead per dependency; "slow" is 80%
    # of this client's own 5s total timeout. The LLD names only payments
    # (50) and inventory (100) explicitly -- cart/catalog get the same
    # 100 as inventory, a reasonable default for a dependency not named.
    bulkheads = {
        "cart-svc": Bulkhead(100),
        "catalog-svc": Bulkhead(100),
        "inventory-svc": Bulkhead(100),
        "payments-svc": Bulkhead(50),
    }
    breakers = {
        dependency: CircuitBreaker(
            service="orders-svc", dependency=dependency, slow_call_duration_s=4.0
        )
        for dependency in bulkheads
    }
    quote_retry_budget = RetryBudget()
    snapshot_retry_budget = RetryBudget()
    participants = service.Participants(
        cart=CartClient(
            http_client,
            base_url=settings.cart_svc_base_url,
            token_source=token_source,
            bulkhead=bulkheads["cart-svc"],
            circuit_breaker=breakers["cart-svc"],
            retry_budget=snapshot_retry_budget,
        ),
        catalog=CatalogClient(
            http_client,
            base_url=settings.catalog_svc_base_url,
            token_source=token_source,
            bulkhead=bulkheads["catalog-svc"],
            circuit_breaker=breakers["catalog-svc"],
            retry_budget=quote_retry_budget,
        ),
        inventory=InventoryClient(
            http_client,
            base_url=settings.inventory_svc_base_url,
            token_source=token_source,
            bulkhead=bulkheads["inventory-svc"],
            circuit_breaker=breakers["inventory-svc"],
        ),
        payments=PaymentsClient(
            http_client,
            base_url=settings.payments_svc_base_url,
            token_source=token_source,
            bulkhead=bulkheads["payments-svc"],
            circuit_breaker=breakers["payments-svc"],
        ),
    )
    app.state.participants = participants

    relay = OutboxRelay(
        service_name="orders-svc",
        session_factory=build_orders_session_factory(relay_engine),
        dsn_asyncpg=_asyncpg_dsn(settings),
        amqp_url=_amqp_url(settings),
        notify_channel="outbox_orders",
    )
    await relay.start()
    app.state.relay = relay

    inventory_session_factory = build_orders_session_factory(inventory_consumer_engine)
    inventory_consumer = InboxConsumer(
        consumer_name=event_handlers.CONSUMER_NAME_INVENTORY,
        session_factory=inventory_session_factory,
        amqp_url=_amqp_url(settings),
        queue_name="q.orders.inventory",
        handlers=event_handlers.build_inventory_dispatch(inventory_session_factory),
    )
    await inventory_consumer.start()
    app.state.inventory_consumer = inventory_consumer

    payments_session_factory = build_orders_session_factory(payments_consumer_engine)
    payments_consumer = InboxConsumer(
        consumer_name=event_handlers.CONSUMER_NAME_PAYMENTS,
        session_factory=payments_session_factory,
        amqp_url=_amqp_url(settings),
        queue_name="q.orders.payments",
        handlers=event_handlers.build_payments_dispatch(
            payments_session_factory, participants.inventory
        ),
    )
    await payments_consumer.start()
    app.state.payments_consumer = payments_consumer

    recovery_worker = RecoveryWorker(
        session_factory=build_orders_session_factory(recovery_engine),
        participants=participants,
        interval_s=settings.recovery_interval_seconds,
        batch_size=settings.recovery_batch_size,
    )
    await recovery_worker.start()
    app.state.recovery_worker = recovery_worker

    yield

    await recovery_worker.stop()
    await payments_consumer.stop()
    await inventory_consumer.stop()
    await relay.stop()
    await watcher.stop()
    await redis.aclose()
    await http_client.aclose()
    await engine.dispose()
    await relay_engine.dispose()
    await inventory_consumer_engine.dispose()
    await payments_consumer_engine.dispose()
    await recovery_engine.dispose()


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
            and app.state.inventory_consumer.is_running
            and app.state.payments_consumer.is_running
            and app.state.recovery_worker.is_running
        )
    except Exception:
        return False


app = FastAPI(title=settings.service_name, lifespan=lifespan)
app.add_middleware(CorrelationIdMiddleware)
install_problem_details(
    app,
    db_busy_errors=POOL_TIMEOUT_ERRORS,
    dependency_unavailable_errors=(CircuitOpenError, BulkheadFullError),
)
app.include_router(
    build_health_router(readiness_checks=[_db_ready, _authz_ready, _background_workers_ready])
)
app.include_router(router)
assert_routes_covered(app)
instrument_fastapi_app(app)
