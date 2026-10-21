"""Structured JSON logs to stdout, carrying trace/span/correlation IDs
(HLD §11.1, LLD-10 §4)."""

import json
import logging
import sys
from datetime import UTC, datetime

from opentelemetry import trace

from .correlation import get_correlation_id
from .settings import BaseServiceSettings

_CONTEXT_FIELDS = ("trace_id", "span_id", "correlation_id")


class _TraceContextFilter(logging.Filter):
    """Stamps the active span's trace/span IDs and the request's correlation
    ID onto every record, so ``_JsonFormatter`` can read them uniformly --
    present or not, in or out of a request/span context."""

    def filter(self, record: logging.LogRecord) -> bool:
        span_context = trace.get_current_span().get_span_context()
        if span_context.is_valid:
            record.trace_id = format(span_context.trace_id, "032x")
            record.span_id = format(span_context.span_id, "016x")
        else:
            record.trace_id = None
            record.span_id = None
        record.correlation_id = get_correlation_id()
        return True


class _JsonFormatter(logging.Formatter):
    def __init__(self, service_name: str, environment: str) -> None:
        super().__init__()
        self._service_name = service_name
        self._environment = environment

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "service": self._service_name,
            "environment": self._environment,
        }
        for field in _CONTEXT_FIELDS:
            payload[field] = getattr(record, field, None)
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def configure_logging(settings: BaseServiceSettings) -> None:
    """Point the root logger at one JSON handler on stdout."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter(settings.service_name, settings.environment))
    handler.addFilter(_TraceContextFilter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level)
