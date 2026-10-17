"""inventory-svc entry point (shared FastAPI template, LLD-12 §1)."""

from fastapi import FastAPI
from sf_common import (
    BaseServiceSettings,
    build_health_router,
    configure_logging,
    install_problem_details,
)

settings = BaseServiceSettings(service_name="inventory-svc")
configure_logging(settings)

app = FastAPI(title=settings.service_name)
install_problem_details(app)
app.include_router(build_health_router())
