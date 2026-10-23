from uuid import UUID

from opentelemetry.sdk.trace import TracerProvider
from sf_messaging import build_audit_data


def _data(**overrides: object) -> dict:
    kwargs: dict = dict(
        source_service="orgs-svc",
        action="org.updated",
        actor_type="user",
        actor_id="actor-1",
        domain="org:abc",
        target_type="organization",
        target_id="abc",
    )
    kwargs.update(overrides)
    return build_audit_data(**kwargs)


def test_matches_the_lld_04_audit_schema() -> None:
    data = _data(reason="rename", changes={"name": {"to": "New"}}, severity="notice")

    UUID(data["audit_id"])  # does not raise
    assert data["actor"] == {"type": "user", "id": "actor-1"}
    assert data["target"] == {"type": "organization", "id": "abc"}
    assert data["domain"] == "org:abc"
    assert data["action"] == "org.updated"
    assert data["result"] == "success"
    assert data["reason"] == "rename"
    assert data["changes"] == {"name": {"to": "New"}}
    assert data["source"] == {"service": "orgs-svc"}
    assert data["severity"] == "notice"


def test_each_call_gets_a_fresh_audit_id() -> None:
    assert _data()["audit_id"] != _data()["audit_id"]


def test_captures_the_active_span_trace_id() -> None:
    tracer = TracerProvider().get_tracer(__name__)
    with tracer.start_as_current_span("request") as span:
        data = _data()

    assert data["trace_id"] == format(span.get_span_context().trace_id, "032x")


def test_trace_id_is_none_outside_any_span() -> None:
    assert _data()["trace_id"] is None
