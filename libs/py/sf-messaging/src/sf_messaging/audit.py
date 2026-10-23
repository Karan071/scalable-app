"""``audit.recorded.v1`` producer helper (LLD-04 §2.1, §2.4).

Every domain service records audit events the same way: an outbox row in
the same transaction as the audited change. One builder for all of them,
because hand-rolled payloads drift -- authz-svc's original one omitted
``audit_id``, and audit-svc's consumer drops any event without it, so none
of its role-change audits were ever stored.

``changes`` is the caller's per-action allow-list (§2.7): never pass
request bodies, tokens, or secrets through it.
"""

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid7

from opentelemetry import trace
from sqlalchemy.ext.asyncio import AsyncSession

from .envelope import build_envelope
from .outbox import add as add_to_outbox

ActorType = Literal["user", "service", "system"]
Severity = Literal["info", "notice", "high"]


def _current_trace_id() -> str | None:
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else None


def build_audit_data(
    *,
    source_service: str,
    action: str,
    actor_type: ActorType,
    actor_id: str,
    domain: str,
    target_type: str,
    target_id: str,
    result: str = "success",
    reason: str | None = None,
    changes: dict[str, Any] | None = None,
    severity: Severity = "info",
    correlation_id: str | None = None,
) -> dict[str, Any]:
    return {
        "audit_id": str(uuid7()),
        "occurred_at": datetime.now(UTC).isoformat(),
        "actor": {"type": actor_type, "id": actor_id},
        "domain": domain,
        "action": action,
        "target": {"type": target_type, "id": target_id},
        "result": result,
        "reason": reason,
        "changes": changes,
        "source": {"service": source_service},
        "correlation_id": correlation_id,
        "trace_id": _current_trace_id(),
        "severity": severity,
    }


async def record_audit(
    session: AsyncSession,
    *,
    notify_channel: str,
    source_service: str,
    action: str,
    actor_type: ActorType,
    actor_id: str,
    domain: str,
    target_type: str,
    target_id: str,
    aggregate_version: int,
    result: str = "success",
    reason: str | None = None,
    changes: dict[str, Any] | None = None,
    severity: Severity = "info",
    correlation_id: str | None = None,
) -> None:
    """Adds one ``audit.recorded.v1`` outbox row. ``target`` doubles as the
    envelope's aggregate, so outbox replay by aggregate finds the audit
    rows alongside the domain events for the same entity."""
    data = build_audit_data(
        source_service=source_service,
        action=action,
        actor_type=actor_type,
        actor_id=actor_id,
        domain=domain,
        target_type=target_type,
        target_id=target_id,
        result=result,
        reason=reason,
        changes=changes,
        severity=severity,
        correlation_id=correlation_id,
    )
    envelope = build_envelope(
        event_type="audit.recorded",
        source=source_service,
        subject=target_id,
        data=data,
        schema_version="1.0",
        aggregate_type=target_type,
        aggregate_version=aggregate_version,
        org_id=domain.removeprefix("org:") if domain.startswith("org:") else None,
        correlation_id=correlation_id,
    )
    await add_to_outbox(
        session,
        envelope,
        exchange="sf.events",
        routing_key="audit.recorded.v1",
        aggregate_id=target_id,
        notify_channel=notify_channel,
    )
