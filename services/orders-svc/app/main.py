"""orders-svc entry point (shared FastAPI template, LLD-12 §1)."""

from fastapi import FastAPI
from sf_common import (
    BaseServiceSettings,
    CorrelationIdMiddleware,
    build_health_router,
    configure_logging,
    init_telemetry,
    install_problem_details,
    instrument_fastapi_app,
)

settings = BaseServiceSettings(service_name="orders-svc")
configure_logging(settings)
init_telemetry(settings)

app = FastAPI(title=settings.service_name)
app.add_middleware(CorrelationIdMiddleware)
install_problem_details(app)
app.include_router(build_health_router())
instrument_fastapi_app(app)
