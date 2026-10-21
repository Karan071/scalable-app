from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from sf_messaging import build_envelope, envelope_id


def test_builds_a_well_formed_cloudevents_envelope() -> None:
    envelope = build_envelope(
        event_type="user.created",
        source="users-svc",
        subject="user-123",
        data={"user_id": "user-123", "status": "active"},
        schema_version="1.0",
        aggregate_type="user",
        aggregate_version=1,
    )

    assert envelope["specversion"] == "1.0"
    assert envelope["type"] == "user.created"
    assert envelope["source"] == "users-svc"
    assert envelope["subject"] == "user-123"
    assert envelope["sfaggregatetype"] == "user"
    assert envelope["sfaggregateversion"] == 1
    assert envelope["data"] == {"user_id": "user-123", "status": "active"}
    assert envelope["time"].endswith("Z")
    assert envelope_id(envelope) is not None


def test_each_call_gets_a_fresh_id() -> None:
    kwargs = dict(
        event_type="user.created",
        source="users-svc",
        subject="user-123",
        data={},
        schema_version="1.0",
        aggregate_type="user",
        aggregate_version=1,
    )

    first = build_envelope(**kwargs)
    second = build_envelope(**kwargs)

    assert first["id"] != second["id"]


def test_captures_the_ambient_trace_context_when_not_passed_explicitly() -> None:
    trace.set_tracer_provider(TracerProvider())
    tracer = trace.get_tracer("test")
    kwargs = dict(
        event_type="user.created",
        source="users-svc",
        subject="user-123",
        data={},
        schema_version="1.0",
        aggregate_type="user",
        aggregate_version=1,
    )

    with tracer.start_as_current_span("request-handler") as span:
        envelope = build_envelope(**kwargs)
        expected_trace_id = format(span.get_span_context().trace_id, "032x")

    assert envelope["traceparent"] is not None
    assert expected_trace_id in envelope["traceparent"]


def test_leaves_traceparent_none_with_no_active_span() -> None:
    trace.set_tracer_provider(TracerProvider())
    envelope = build_envelope(
        event_type="user.created",
        source="users-svc",
        subject="user-123",
        data={},
        schema_version="1.0",
        aggregate_type="user",
        aggregate_version=1,
    )

    assert envelope["traceparent"] is None


def test_an_explicit_traceparent_is_not_overridden() -> None:
    trace.set_tracer_provider(TracerProvider())
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("request-handler"):
        envelope = build_envelope(
            event_type="user.created",
            source="users-svc",
            subject="user-123",
            data={},
            schema_version="1.0",
            aggregate_type="user",
            aggregate_version=1,
            traceparent="explicit-value",
        )

    assert envelope["traceparent"] == "explicit-value"
