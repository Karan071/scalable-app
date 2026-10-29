"""Shared settings, logging, HTTP, problem-details, crypto, health, and resilience helpers.
No domain logic."""

from .bulkhead import Bulkhead, BulkheadFullError
from .cache import CacheClient, CacheHit
from .circuit_breaker import CircuitBreaker, CircuitOpenError, CircuitState
from .correlation import CorrelationIdMiddleware, get_correlation_id
from .crypto import TokenCipher
from .deadlines import DeadlineExceeded, new_deadline_ms, read_deadline_header, require_remaining_ms
from .health import build_health_router
from .http import create_http_client
from .logging import configure_logging
from .problems import install_problem_details
from .retry import RetryBudget, send_with_retries
from .settings import BaseServiceSettings
from .telemetry import init_telemetry, instrument_fastapi_app

__all__ = [
    "BaseServiceSettings",
    "Bulkhead",
    "BulkheadFullError",
    "CacheClient",
    "CacheHit",
    "CircuitBreaker",
    "CircuitOpenError",
    "CircuitState",
    "CorrelationIdMiddleware",
    "DeadlineExceeded",
    "RetryBudget",
    "TokenCipher",
    "build_health_router",
    "configure_logging",
    "create_http_client",
    "get_correlation_id",
    "init_telemetry",
    "install_problem_details",
    "new_deadline_ms",
    "read_deadline_header",
    "require_remaining_ms",
    "send_with_retries",
    "instrument_fastapi_app",
]
