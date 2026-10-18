"""authz-svc entry point (LLD-02)."""

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
    build_health_router,
    configure_logging,
    create_http_client,
    install_problem_details,
)
from sqlalchemy import text

from .db import build_authz_engine, build_authz_reader_enforcer, build_authz_session_factory
from .orgs_client import OrgsClient, ServiceTokenSource
from .routes import router
from .settings import Settings

settings = Settings()
configure_logging(settings)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    engine = build_authz_engine(settings)
    http_client = create_http_client()
    redis: Redis = Redis.from_url(settings.redis_cache_url, decode_responses=True)
    enforcer = build_authz_reader_enforcer(settings)
    enforcer.load()  # synchronous; readiness waits on this below

    app.state.settings = settings
    app.state.session_factory = build_authz_session_factory(engine)
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

    yield

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


app = FastAPI(title=settings.service_name, lifespan=lifespan)
install_problem_details(app)
app.include_router(build_health_router(readiness_checks=[_db_ready, _authz_ready]))
app.include_router(router)
assert_routes_covered(app)
