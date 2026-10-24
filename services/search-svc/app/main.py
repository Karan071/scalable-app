"""search-svc entry point (LLD-05 §7-§8).

No Casbin enforcer, no token validator, no outbox relay: every route is
``Public`` (nothing here checks a session or a service token), and this
service publishes no events of its own -- it only consumes catalog-svc's.
"""

import urllib.parse
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sf_authz import assert_routes_covered
from sf_common import (
    CorrelationIdMiddleware,
    build_health_router,
    configure_logging,
    init_telemetry,
    install_problem_details,
    instrument_fastapi_app,
)
from sf_db import POOL_TIMEOUT_ERRORS, worker_pool
from sf_messaging import DEFAULT_HANDLER_CONCURRENCY, InboxConsumer
from sqlalchemy import text

from . import event_handlers
from .db import build_search_engine, build_search_session_factory
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
    engine = build_search_engine(settings)
    # LLD-09 §2 bulkheads: requests and the consumer get their own pool, so
    # a backlog in one can't starve the other.
    catalog_consumer_engine = build_search_engine(
        settings, **worker_pool(DEFAULT_HANDLER_CONCURRENCY)
    )
    session_factory = build_search_session_factory(engine)
    app.state.settings = settings
    app.state.session_factory = session_factory

    consumer = InboxConsumer(
        consumer_name=event_handlers.CONSUMER_NAME,
        session_factory=build_search_session_factory(catalog_consumer_engine),
        amqp_url=_amqp_url(settings),
        queue_name="q.search.catalog",
        handlers=event_handlers.DISPATCH,
    )
    await consumer.start()
    app.state.consumer = consumer

    yield

    await consumer.stop()
    await engine.dispose()
    await catalog_consumer_engine.dispose()


async def _db_ready() -> bool:
    try:
        async with app.state.session_factory() as session:
            await session.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


async def _consumer_ready() -> bool:
    try:
        return app.state.consumer.is_running
    except Exception:
        return False


app = FastAPI(title=settings.service_name, lifespan=lifespan)
app.add_middleware(CorrelationIdMiddleware)
install_problem_details(app, db_busy_errors=POOL_TIMEOUT_ERRORS)
app.include_router(build_health_router(readiness_checks=[_db_ready, _consumer_ready]))
app.include_router(router)
assert_routes_covered(app)
instrument_fastapi_app(app)
