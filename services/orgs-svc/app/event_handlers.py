"""orgs-svc's InboxConsumer dispatch table (LLD-08 §2.1's ``q.orgs.users``
binding: the only identity event orgs-svc consumes is ``user.disabled``).

Disabling a user account must disable their memberships in every org too --
this is distinct from authz-svc's own ``user.disabled`` consumer, which only
suspends role assignments (LLD-02 §9), and from the BFF's, which only
revokes sessions (LLD-01 §8). Without this handler, a disabled user would
stay listed as an active member of every org they belonged to.

``sf_messaging.InboxConsumer`` already wraps the call in ``session.begin()``
and only commits + acks on a clean return, so this handler assumes it's
inside an open transaction and must not commit/rollback itself.
"""

from uuid import UUID

from sf_messaging import add_to_outbox, build_envelope
from sqlalchemy.ext.asyncio import AsyncSession

from . import repository, service

CONSUMER_NAME = "orgs.users"

_NOTIFY_CHANNEL = "outbox_orgs"


async def handle_user_disabled(session: AsyncSession, envelope: dict) -> None:
    data = envelope["data"]
    user_id = UUID(data["user_id"])
    # user.disabled carries no actor_id (users-svc's own envelope has none) --
    # same fallback authz-svc's identical consumer already uses.
    actor_id = UUID(data.get("actor_id") or data["user_id"])

    disabled = await repository.disable_all_active_memberships_for_user(
        session, user_id=user_id, reason="user_disabled"
    )
    for membership in disabled:
        out_envelope = build_envelope(
            event_type="membership.disabled",
            source="orgs-svc",
            subject=str(membership.id),
            data={
                "membership_id": str(membership.id),
                "org_id": str(membership.org_id),
                "user_id": str(user_id),
                "actor_id": str(actor_id),
            },
            schema_version="1.0",
            aggregate_type="membership",
            aggregate_version=membership.version,
        )
        await add_to_outbox(
            session,
            out_envelope,
            exchange="sf.events",
            routing_key="membership.disabled.v1",
            aggregate_id=str(membership.id),
            notify_channel=_NOTIFY_CHANNEL,
        )
        # No human acted on this membership: it's a cascade of the user's
        # own disable, which users-svc audits separately.
        await service.audit(
            session,
            action="membership.disabled",
            actor_type="system",
            actor_id="orgs-svc",
            org_id=membership.org_id,
            target_type="membership",
            target_id=membership.id,
            aggregate_version=membership.version,
            reason="user_disabled",
            changes={"user_id": {"to": str(user_id)}, "status": {"to": membership.status}},
            severity="notice",
        )


DISPATCH = {
    "user.disabled": handle_user_disabled,
}
