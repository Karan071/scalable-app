"""authz-svc's InboxConsumer dispatch table (LLD-02 §9, LLD-08 §6).

Every function here is one idempotent-via-inbox transaction:
``sf_messaging.InboxConsumer`` already wraps the call in ``session.begin()``
and only commits + acks on a clean return, so these functions assume
they're inside an open transaction and must not commit/rollback themselves.

Each handler returns the new ``policy_version`` (``int``) if it changed the
compiled Casbin policy, else ``None``. ``InboxConsumer``'s ``on_handled``
hook (wired in ``main.py``) publishes that version to ``authz:policy`` on
redis-cache after the commit, exactly like ``routes.py``'s HTTP-driven path
already does — so event-driven grants propagate to other processes'
watchers just as promptly as HTTP-driven ones.

Deliberate correction to LLD-02 §9's literal wording: that table says
"Bump the version" explicitly for every row except ``organization.reactivated``.
Implementing that omission literally would be a real bug, not a faithful
reading — ``policy_version`` is the *only* signal ``PolicyWatcher`` has for
"reload your cached snapshot" (both its redis-cache pub/sub path and its
10s polling fallback key off it), and ``domain_status`` is part of that
same cached snapshot. Skipping the bump on reactivation would mean a
reactivated org stays permanently "suspended" in every other process's
enforcer until an unrelated policy change happens to bump the version
elsewhere, or the process restarts. So ``organization.reactivated`` bumps
and returns the version too, same as every other domain_status-changing
transition.
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from . import repository
from .events import emit_role_changed

CONSUMER_NAME = "authz.identity"


async def _bump_and_return_version_if(session: AsyncSession, changed: bool) -> int | None:
    if not changed:
        return None
    return await repository.bump_policy_version(session)


async def handle_membership_created(session: AsyncSession, envelope: dict) -> int | None:
    """LLD-02 §9: "Upsert the projection. Grant the roles requested by the
    accepted invitation (if any)." — unconditional grant, no ``role_grant``
    re-check. See ``service.py``'s module docstring for why: a brand-new
    org's owner invitation has no existing grant-holder to check against
    (platform roles don't match an org domain), so requiring a check here
    would make it impossible for any org to ever get its first org_admin.
    """
    data = envelope["data"]
    membership_id = UUID(data["membership_id"])
    org_id = UUID(data["org_id"])
    user_id = UUID(data["user_id"])
    invited_by = UUID(data["invited_by"])
    domain = f"org:{org_id}"

    await repository.upsert_membership_projection(
        session,
        membership_id=membership_id,
        org_id=org_id,
        user_id=user_id,
        status="active",
        source_version=envelope["sfaggregateversion"],
    )

    granted_any = False
    for role_id in data.get("roles_requested") or []:
        assignment = await repository.grant_role_unconditional(
            session,
            user_id=user_id,
            role_id=role_id,
            domain=domain,
            org_id=org_id,
            membership_id=membership_id,
            granted_by=invited_by,
            reason="invitation_accepted",
        )
        if assignment is not None:
            granted_any = True
            await emit_role_changed(session, assignment, actor_id=invited_by, change="granted")

    return await _bump_and_return_version_if(session, granted_any)


async def handle_membership_disabled(session: AsyncSession, envelope: dict) -> int | None:
    """LLD-02 §9: "Projection -> disabled. Active assignments for (user, org)
    -> suspended. Remove g rows. Bump the version." """
    data = envelope["data"]
    membership_id = UUID(data["membership_id"])
    org_id = UUID(data["org_id"])
    user_id = UUID(data["user_id"])
    actor_id = UUID(data["actor_id"])

    await repository.upsert_membership_projection(
        session,
        membership_id=membership_id,
        org_id=org_id,
        user_id=user_id,
        status="disabled",
        source_version=envelope["sfaggregateversion"],
    )

    suspended = await repository.suspend_role_assignments_for_membership(
        session, user_id=user_id, org_id=org_id, reason="membership_disabled"
    )
    for assignment in suspended:
        await repository.delete_casbin_g_row(
            session, user_id=user_id, role_id=assignment.role_id, domain=assignment.domain
        )
        await emit_role_changed(session, assignment, actor_id=actor_id, change="suspended")

    return await _bump_and_return_version_if(session, bool(suspended))


async def handle_membership_reactivated(session: AsyncSession, envelope: dict) -> int | None:
    """LLD-02 §9: "Projection -> active. Assignments suspended by membership
    disable -> active. Re-add g rows." """
    data = envelope["data"]
    membership_id = UUID(data["membership_id"])
    org_id = UUID(data["org_id"])
    user_id = UUID(data["user_id"])
    actor_id = UUID(data["actor_id"])

    await repository.upsert_membership_projection(
        session,
        membership_id=membership_id,
        org_id=org_id,
        user_id=user_id,
        status="active",
        source_version=envelope["sfaggregateversion"],
    )

    reactivated = await repository.reactivate_role_assignments_suspended_for_reason(
        session, user_id=user_id, org_id=org_id, reason="membership_disabled"
    )
    for assignment in reactivated:
        await repository.insert_casbin_g_row(
            session, user_id=user_id, role_id=assignment.role_id, domain=assignment.domain
        )
        await emit_role_changed(session, assignment, actor_id=actor_id, change="reactivated")

    return await _bump_and_return_version_if(session, bool(reactivated))


async def handle_membership_revoked(session: AsyncSession, envelope: dict) -> int | None:
    """LLD-02 §9: "Projection -> revoked. Assignments -> revoked." """
    data = envelope["data"]
    membership_id = UUID(data["membership_id"])
    org_id = UUID(data["org_id"])
    user_id = UUID(data["user_id"])
    actor_id = UUID(data["actor_id"])

    await repository.upsert_membership_projection(
        session,
        membership_id=membership_id,
        org_id=org_id,
        user_id=user_id,
        status="revoked",
        source_version=envelope["sfaggregateversion"],
    )

    revoked = await repository.revoke_role_assignments_for_membership(
        session, user_id=user_id, org_id=org_id, reason="membership_revoked"
    )
    for assignment in revoked:
        await repository.delete_casbin_g_row(
            session, user_id=user_id, role_id=assignment.role_id, domain=assignment.domain
        )
        await emit_role_changed(session, assignment, actor_id=actor_id, change="revoked")

    return await _bump_and_return_version_if(session, bool(revoked))


async def _handle_organization_status(
    session: AsyncSession, envelope: dict, *, status: str
) -> int | None:
    data = envelope["data"]
    org_id = UUID(data["org_id"])
    row = await repository.upsert_domain_status(
        session,
        domain=f"org:{org_id}",
        status=status,
        source_version=envelope["sfaggregateversion"],
    )
    return await _bump_and_return_version_if(session, row is not None)


async def handle_organization_suspended(session: AsyncSession, envelope: dict) -> int | None:
    return await _handle_organization_status(session, envelope, status="suspended")


async def handle_organization_archived(session: AsyncSession, envelope: dict) -> int | None:
    return await _handle_organization_status(session, envelope, status="archived")


async def handle_organization_reactivated(session: AsyncSession, envelope: dict) -> int | None:
    # LLD-02 §9 literally omits "bump the version" for this row alone; see
    # this module's docstring for why that omission isn't implemented as
    # written — domain_status is cached by every enforcer the same way
    # casbin_rule is, so a reactivation that doesn't bump would never
    # actually become visible anywhere.
    return await _handle_organization_status(session, envelope, status="active")


async def handle_user_created(session: AsyncSession, envelope: dict) -> int | None:
    """LLD-02 §9: "Grant role:customer in customer." """
    data = envelope["data"]
    user_id = UUID(data["user_id"])
    assignment = await repository.grant_role_unconditional(
        session,
        user_id=user_id,
        role_id="customer",
        domain="customer",
        org_id=None,
        membership_id=None,
        granted_by=user_id,
        reason="signup",
    )
    if assignment is None:
        return None
    await emit_role_changed(session, assignment, actor_id=user_id, change="granted")
    return await repository.bump_policy_version(session)


async def handle_user_disabled(session: AsyncSession, envelope: dict) -> int | None:
    """LLD-02 §9: "All assignments -> suspended (reason user_disabled)." """
    data = envelope["data"]
    user_id = UUID(data["user_id"])
    actor_id = UUID(data.get("actor_id") or data["user_id"])

    suspended = await repository.suspend_all_role_assignments_for_user(
        session, user_id=user_id, reason="user_disabled"
    )
    for assignment in suspended:
        await repository.delete_casbin_g_row(
            session, user_id=user_id, role_id=assignment.role_id, domain=assignment.domain
        )
        await emit_role_changed(session, assignment, actor_id=actor_id, change="suspended")

    return await _bump_and_return_version_if(session, bool(suspended))


async def handle_user_reactivated(session: AsyncSession, envelope: dict) -> int | None:
    """LLD-02 §9: "role:customer restored. Others stay suspended until
    re-granted." — only the customer role comes back; nothing else is
    reactivated by this event."""
    data = envelope["data"]
    user_id = UUID(data["user_id"])

    reactivated = await repository.reactivate_role_assignments_suspended_for_reason(
        session, user_id=user_id, org_id=None, reason="user_disabled", role_id="customer"
    )
    if reactivated:
        for assignment in reactivated:
            await repository.insert_casbin_g_row(
                session, user_id=user_id, role_id="customer", domain="customer"
            )
            await emit_role_changed(session, assignment, actor_id=user_id, change="reactivated")
        return await repository.bump_policy_version(session)

    # The customer role may have been deleted rather than merely suspended
    # (it's never explicitly deleted by any handler here, so in practice
    # this branch only fires if the row never existed at all) — re-grant.
    granted = await repository.grant_role_unconditional(
        session,
        user_id=user_id,
        role_id="customer",
        domain="customer",
        org_id=None,
        membership_id=None,
        granted_by=user_id,
        reason="reactivated",
    )
    if granted is None:
        return None
    await emit_role_changed(session, granted, actor_id=user_id, change="granted")
    return await repository.bump_policy_version(session)


DISPATCH = {
    "membership.created": handle_membership_created,
    "membership.disabled": handle_membership_disabled,
    "membership.reactivated": handle_membership_reactivated,
    "membership.revoked": handle_membership_revoked,
    "organization.suspended": handle_organization_suspended,
    "organization.archived": handle_organization_archived,
    "organization.reactivated": handle_organization_reactivated,
    "user.created": handle_user_created,
    "user.disabled": handle_user_disabled,
    "user.reactivated": handle_user_reactivated,
    # Deliberately absent: organization.created, organization.updated —
    # InboxConsumer's missing-handler path no-ops these (they arrive only
    # because q.authz.identity is bound on the organization.*.v1 wildcard).
}
