import json
import logging

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sf_common.logging import configure_logging
from sf_common.settings import BaseServiceSettings


def _settings() -> BaseServiceSettings:
    return BaseServiceSettings(service_name="test-svc", environment="test")


def _emit_and_capture(capsys, log_fn) -> dict:
    configure_logging(_settings())
    log_fn()
    out = capsys.readouterr().out.strip().splitlines()[-1]
    return json.loads(out)


def test_log_line_has_null_trace_fields_outside_a_span(capsys) -> None:
    payload = _emit_and_capture(capsys, lambda: logging.getLogger("x").info("hello"))

    assert payload["trace_id"] is None
    assert payload["span_id"] is None
    assert payload["message"] == "hello"
    assert payload["service"] == "test-svc"


def test_log_line_carries_the_active_spans_trace_and_span_id(capsys) -> None:
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    tracer = trace.get_tracer("test")

    configure_logging(_settings())
    with tracer.start_as_current_span("test-span") as span:
        logging.getLogger("x").info("inside a span")
        expected_trace_id = format(span.get_span_context().trace_id, "032x")
        expected_span_id = format(span.get_span_context().span_id, "016x")

    out = capsys.readouterr().out.strip().splitlines()[-1]
    payload = json.loads(out)

    assert payload["trace_id"] == expected_trace_id
    assert payload["span_id"] == expected_span_id
