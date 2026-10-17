"""Structured JSON logs to stdout, carrying trace/span IDs (HLD §11.1)."""

import json
import logging
import sys
from datetime import UTC, datetime

from .settings import BaseServiceSettings

_TRACE_FIELDS = ("trace_id", "span_id")


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
        for field in _TRACE_FIELDS:
            payload[field] = getattr(record, field, None)
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def configure_logging(settings: BaseServiceSettings) -> None:
    """Point the root logger at one JSON handler on stdout."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter(settings.service_name, settings.environment))

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level)
