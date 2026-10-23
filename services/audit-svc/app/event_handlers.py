"""audit-svc's InboxConsumer dispatch table (LLD-04 §2.3, LLD-08 §6).

Each handler runs inside InboxConsumer's open transaction. It must not
commit/rollback itself. It returns ``None`` always (audit-svc does not
manage a policy_version; ``on_handled`` is wired to None in main.py).

The hash chain is maintained by ``repository.insert_audit_event``, which
acquires ``pg_advisory_xact_lock`` before every insert — so even if two
InboxConsumer workers were processing messages concurrently (they share the
same queue), the lock serialises the actual insert, preserving chain
integrity without requiring a single-threaded consumer.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from . import repository

CONSUMER_NAME = "audit.ingest"


def _trace_id_from_traceparent(traceparent: str | None) -> str | None:
    # W3C traceparent: "<version>-<trace-id>-<parent-id>-<flags>"
    parts = (traceparent or "").split("-")
    return parts[1] if len(parts) == 4 else None


async def handle_audit_recorded(session: AsyncSession, envelope: dict) -> None:
    data: dict = envelope.get("data", {})

    audit_id_raw = data.get("audit_id")
    if audit_id_raw is None:
        # Malformed event — no audit_id to insert; ack without recording.
        return

    audit_id = uuid.UUID(str(audit_id_raw))

    occurred_at_raw = data.get("occurred_at") or envelope.get("time")
    if occurred_at_raw:
        occurred_at = datetime.fromisoformat(str(occurred_at_raw).replace("Z", "+00:00"))
    else:
        occurred_at = datetime.now(UTC)

    on_behalf_of_raw = data.get("on_behalf_of")
    on_behalf_of = uuid.UUID(str(on_behalf_of_raw)) if on_behalf_of_raw else None

    await repository.insert_audit_event(
        session,
        audit_id=audit_id,
        occurred_at=occurred_at,
        actor=data.get("actor", {}),
        on_behalf_of=on_behalf_of,
        domain=str(data.get("domain", "unknown")),
        action=str(data.get("action", "unknown")),
        target=data.get("target", {}),
        result=str(data.get("result", "success")),
        reason=data.get("reason"),
        changes=data.get("changes"),
        source=data.get("source", {}),
        correlation_id=data.get("correlation_id") or envelope.get("sfcorrelationid"),
        trace_id=data.get("trace_id") or _trace_id_from_traceparent(envelope.get("traceparent")),
        client=data.get("client"),
        severity=str(data.get("severity", "info")),
    )


DISPATCH = {
    "audit.recorded": handle_audit_recorded,
}
