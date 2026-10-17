"""users-svc entry point (LLD-01 §11)."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sf_authz import TokenValidator, install_token_validator
from sf_common import (
    build_health_router,
    configure_logging,
    install_problem_details,
)
from sqlalchemy import text

from .db import build_users_engine, build_users_session_factory
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
    yield
    await engine.dispose()


async def _db_ready() -> bool:
    try:
        async with app.state.session_factory() as session:
            await session.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


app = FastAPI(title=settings.service_name, lifespan=lifespan)
install_problem_details(app)
app.include_router(build_health_router(readiness_checks=[_db_ready]))
app.include_router(router)
