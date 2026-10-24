"""cart-svc entry point (LLD-06 §2).

No outbox, no inbox consumer: this slice publishes nothing (no other
service needs a cart event yet) and consumes nothing (the
``order.confirmed.v1`` subtract-on-purchase rule waits on orders-svc, which
doesn't exist yet -- an open item, not an oversight).
"""

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
    CorrelationIdMiddleware,
    build_health_router,
    configure_logging,
    init_telemetry,
    install_problem_details,
    instrument_fastapi_app,
)
from sf_db import POOL_TIMEOUT_ERRORS
from sqlalchemy import text

from .catalog_client import CatalogClient, ServiceTokenSource
from .db import build_cart_engine, build_cart_session_factory
from .routes import router
from .settings import Settings

settings = Settings()
configure_logging(settings)
init_telemetry(settings)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    engine = build_cart_engine(settings)
    session_factory = build_cart_session_factory(engine)
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
    app.state.catalog_client = CatalogClient(
        http_client,
        base_url=settings.catalog_svc_base_url,
        token_source=ServiceTokenSource(
            http_client,
            token_endpoint=settings.oidc_token_endpoint,
            client_id=settings.svc_cart_client_id,
            client_secret=settings.svc_cart_client_secret,
        ),
    )

    yield

    await watcher.stop()
    await redis.aclose()
    await http_client.aclose()
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


app = FastAPI(title=settings.service_name, lifespan=lifespan)
app.add_middleware(CorrelationIdMiddleware)
install_problem_details(app, db_busy_errors=POOL_TIMEOUT_ERRORS)
app.include_router(build_health_router(readiness_checks=[_db_ready, _authz_ready]))
app.include_router(router)
assert_routes_covered(app)
instrument_fastapi_app(app)
