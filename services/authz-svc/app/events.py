"""Outbox event emission, shared by the HTTP-driven write path (``service.py``)
and the event-driven one (``event_handlers.py``)."""

from uuid import UUID

from sf_messaging import add_to_outbox, build_envelope
from sqlalchemy.ext.asyncio import AsyncSession

from .models import RoleAssignment

_NOTIFY_CHANNEL = "outbox_authz"


async def emit(
    session: AsyncSession,
    *,
    event_type: str,
    routing_key: str,
    aggregate_type: str,
    aggregate_id: UUID,
    aggregate_version: int,
    data: dict[str, object],
    exchange: str = "sf.events",
) -> None:
    envelope = build_envelope(
        event_type=event_type,
        source="authz-svc",
        subject=str(aggregate_id),
        data=data,
        schema_version="1.0",
        aggregate_type=aggregate_type,
        aggregate_version=aggregate_version,
    )
    await add_to_outbox(
        session,
        envelope,
        exchange=exchange,
        routing_key=routing_key,
        aggregate_id=str(aggregate_id),
        notify_channel=_NOTIFY_CHANNEL,
    )


async def emit_role_changed(
    session: AsyncSession, assignment: RoleAssignment, *, actor_id: UUID, change: str
) -> None:
    await emit(
        session,
        event_type="membership.role_changed",
        routing_key="membership.role_changed.v1",
        aggregate_type="role_assignment",
        aggregate_id=assignment.id,
        aggregate_version=assignment.version,
        data={
            "org_id": str(assignment.org_id) if assignment.org_id else None,
            "domain": assignment.domain,
            "user_id": str(assignment.user_id),
            "role_id": assignment.role_id,
            "change": change,
            "actor_id": str(actor_id),
        },
    )
    await emit(
        session,
        event_type="audit.recorded",
        routing_key="audit.recorded.v1",
        aggregate_type="role_assignment",
        aggregate_id=assignment.id,
        aggregate_version=assignment.version,
        data={
            "action": f"role_assignment.{change}",
            "actor_id": str(actor_id),
            "target_user_id": str(assignment.user_id),
            "role_id": assignment.role_id,
            "domain": assignment.domain,
        },
    )


async def emit_mfa_group_sync(
    session: AsyncSession, assignment: RoleAssignment, *, required: bool
) -> None:
    """A command (LLD-08 §4.2), not an event -- goes to ``sf.commands``, not
    ``sf.events`` like every other ``emit*`` in this module. Getting this
    wrong means the message is unroutable (no ``sf.events`` binding matches
    this routing key) and the outbox row never leaves ``published_at IS
    NULL`` (confirmed: this was the bug before this comment existed).

    ``required`` carries the desired end state (in the group / out of it),
    computed by the caller at emit time -- not just "add", despite LLD-01
    §9's accepted text only describing the grant/add direction. Without a
    ``required=False`` case, a user whose last ``requires_mfa`` role is
    revoked would stay in Keycloak's ``mfa-required`` group forever (LLD-01
    §9 itself notes the group is a UX nudge, not the security boundary --
    ``acr`` is re-checked at enforcement time regardless -- but leaving a
    de-privileged user permanently forced through MFA setup is still worth
    fixing)."""
    await emit(
        session,
        event_type="identity.mfa_group_sync_requested",
        routing_key="identity.mfa_group_sync_requested.v1",
        exchange="sf.commands",
        aggregate_type="role_assignment",
        aggregate_id=assignment.id,
        aggregate_version=assignment.version,
        data={"user_id": str(assignment.user_id), "required": required},
    )
