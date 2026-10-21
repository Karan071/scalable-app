"""CloudEvents-shaped event envelope (LLD-08 §3)."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid7

from opentelemetry import propagate


def build_envelope(
    *,
    event_type: str,
    source: str,
    subject: str,
    data: dict[str, Any],
    schema_version: str,
    aggregate_type: str,
    aggregate_version: int,
    org_id: str | None = None,
    correlation_id: str | None = None,
    causation_id: str | None = None,
    traceparent: str | None = None,
    tracestate: str | None = None,
) -> dict[str, Any]:
    """Build one envelope. ``id`` doubles as the outbox row id and AMQP
    ``message_id``. When the caller doesn't pass ``traceparent`` explicitly,
    it's captured from whatever span is active at call time (LLD-08 §10) --
    normally the HTTP request handler that's writing this outbox row -- so
    the eventual consumer's span links back to the request that caused it."""
    if traceparent is None:
        carrier: dict[str, str] = {}
        propagate.inject(carrier)
        traceparent = carrier.get("traceparent")
        tracestate = carrier.get("tracestate", tracestate)

    event_id = uuid7()
    occurred_at = datetime.now(UTC)
    return {
        "specversion": "1.0",
        "id": str(event_id),
        "type": event_type,
        "source": source,
        "subject": subject,
        "time": occurred_at.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "datacontenttype": "application/json",
        "dataschema": f"https://schemas.scaleforge.dev/events/{event_type}/v1.json",
        "sfschemaversion": schema_version,
        "sfaggregatetype": aggregate_type,
        "sfaggregateversion": aggregate_version,
        "sforgid": org_id,
        "sfcorrelationid": correlation_id,
        "sfcausationid": causation_id,
        "traceparent": traceparent,
        "tracestate": tracestate or "",
        "data": data,
    }


def envelope_id(envelope: dict[str, Any]) -> UUID:
    return UUID(envelope["id"])
