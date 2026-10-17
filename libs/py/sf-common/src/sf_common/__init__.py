"""Shared settings, logging, HTTP, problem-details, and health helpers. No domain logic."""

from .health import build_health_router
from .http import create_http_client
from .logging import configure_logging
from .problems import install_problem_details
from .settings import BaseServiceSettings

__all__ = [
    "BaseServiceSettings",
    "build_health_router",
    "configure_logging",
    "create_http_client",
    "install_problem_details",
]
