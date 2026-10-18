"""users-svc entry point (LLD-01 §11)."""

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
    build_health_router,
    configure_logging,
    install_problem_details,
)
from sqlalchemy import text

from .db import build_users_engine, build_users_session_factory
from .keycloak_admin import KeycloakAdminClient
from .routes import router
from .settings import Settings

settings = Settings()
configure_logging(settings)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    engine = build_users_engine(settings)
    app.state.session_factory = build_users_session_factory(engine)
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
    app.state.keycloak_admin = KeycloakAdminClient(
        http_client,
        admin_base_url=settings.keycloak_admin_base_url,
        token_endpoint=f"{settings.oidc_issuer}/protocol/openid-connect/token",
        client_id=settings.keycloak_admin_client_id,
        client_secret=settings.keycloak_admin_client_secret,
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
install_problem_details(app)
app.include_router(build_health_router(readiness_checks=[_db_ready, _authz_ready]))
app.include_router(router)
assert_routes_covered(app)
