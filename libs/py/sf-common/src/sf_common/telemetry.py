"""OpenTelemetry SDK bootstrap (LLD-10 §2-§4).

Telemetry is never a hard dependency (§12): ``OTLPSpanExporter``'s own
``BatchSpanProcessor`` exports off a background thread and drops/retries
silently on a connection failure, so an unreachable collector/Jaeger never
blocks or fails service startup. Reads the standard ``OTEL_*`` env vars
(``OTEL_EXPORTER_OTLP_ENDPOINT`` etc.), not ``SF_``-prefixed ones — these are
OpenTelemetry SDK conventions, not this project's own config surface.
"""

import logging
import os

from fastapi import FastAPI
from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.instrumentation.system_metrics import SystemMetricsInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricReader, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from .settings import BaseServiceSettings

_EXCLUDED_URLS = "/healthz,/readyz,/livez,/metrics"

# Per-export deadline, retries included. The SDK's 10 s default made a
# process with no reachable Collector take ~30 s to exit (final metric and
# span flushes): CI has no Collector, and in production a Collector outage
# would eat the pod's graceful-shutdown budget (LLD-10 §12). The Collector
# is local (Compose) or node-local (DaemonSet), so 2 s is ample.
_OTLP_EXPORT_TIMEOUT_S = 2.0

_logger = logging.getLogger(__name__)


def init_telemetry(settings: BaseServiceSettings) -> None:
    """Sets the global tracer and meter providers and instruments
    httpx/redis/system metrics process-wide. Call once at startup, before
    building any httpx client or Redis connection so their spans are
    captured from the first use, and before ``instrument_fastapi_app`` so the
    HTTP server histograms (LLD-10 §7) bind to the real meter provider.
    """
    # LLD-10 §7 names the stable HTTP semconv (`http.server.request.duration`,
    # seconds); the instrumentations default to the old one
    # (`http.server.duration`, ms) unless opted in before instrumenting.
    os.environ.setdefault("OTEL_SEMCONV_STABILITY_OPT_IN", "http")

    resource = Resource.create(
        {
            "service.name": settings.service_name,
            "service.namespace": "scaleforge",
            "deployment.environment.name": settings.environment,
        }
    )
    provider = TracerProvider(resource=resource)
    try:
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(timeout=_OTLP_EXPORT_TIMEOUT_S))
        )
    except Exception:
        _logger.warning("otel_exporter_init_failed", exc_info=True)
    trace.set_tracer_provider(provider)

    # Export interval comes from OTEL_METRIC_EXPORT_INTERVAL (SDK default 60 s).
    readers: list[MetricReader] = []
    try:
        readers.append(
            PeriodicExportingMetricReader(OTLPMetricExporter(timeout=_OTLP_EXPORT_TIMEOUT_S))
        )
    except Exception:
        _logger.warning("otel_metric_exporter_init_failed", exc_info=True)
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=readers))

    HTTPXClientInstrumentor().instrument()
    RedisInstrumentor().instrument()
    SystemMetricsInstrumentor().instrument()


def instrument_fastapi_app(app: FastAPI) -> None:
    """Per-app instrumentation (needs the app instance, unlike the
    process-wide calls in ``init_telemetry``). Excludes health/metrics
    endpoints from tracing (LLD-10 §2) so they don't drown real traffic."""
    FastAPIInstrumentor.instrument_app(app, excluded_urls=_EXCLUDED_URLS)
