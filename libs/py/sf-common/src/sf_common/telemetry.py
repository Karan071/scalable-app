"""OpenTelemetry SDK bootstrap (LLD-10 §2-§4).

Telemetry is never a hard dependency (§12): ``OTLPSpanExporter``'s own
``BatchSpanProcessor`` exports off a background thread and drops/retries
silently on a connection failure, so an unreachable collector/Jaeger never
blocks or fails service startup. Reads the standard ``OTEL_*`` env vars
(``OTEL_EXPORTER_OTLP_ENDPOINT`` etc.), not ``SF_``-prefixed ones — these are
OpenTelemetry SDK conventions, not this project's own config surface.
"""

import logging

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.instrumentation.system_metrics import SystemMetricsInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from .settings import BaseServiceSettings

_EXCLUDED_URLS = "/healthz,/readyz,/livez,/metrics"

_logger = logging.getLogger(__name__)


def init_telemetry(settings: BaseServiceSettings) -> None:
    """Sets the global tracer provider and instruments httpx/redis/system
    metrics process-wide. Call once at startup, before building any httpx
    client or Redis connection so their spans are captured from the first use.
    """
    resource = Resource.create(
        {
            "service.name": settings.service_name,
            "service.namespace": "scaleforge",
            "deployment.environment.name": settings.environment,
        }
    )
    provider = TracerProvider(resource=resource)
    try:
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    except Exception:
        _logger.warning("otel_exporter_init_failed", exc_info=True)
    trace.set_tracer_provider(provider)

    HTTPXClientInstrumentor().instrument()
    RedisInstrumentor().instrument()
    SystemMetricsInstrumentor().instrument()


def instrument_fastapi_app(app: FastAPI) -> None:
    """Per-app instrumentation (needs the app instance, unlike the
    process-wide calls in ``init_telemetry``). Excludes health/metrics
    endpoints from tracing (LLD-10 §2) so they don't drown real traffic."""
    FastAPIInstrumentor.instrument_app(app, excluded_urls=_EXCLUDED_URLS)
