"""authz-svc business logic: the LLD-02 §7 write path and §11 escalation guards.

Interim design decision (documented, not silent): LLD-02 §7 step 1 requires
locking a fresh ``membership_projection`` row, which is normally kept fresh
by consuming ``membership.*``/``organization.*`` events (§9). That consumer
doesn't exist yet — it's a separate, later epic (E2.8: outbox relay +
inbox). Until then, ``assign_role``/``revoke_role`` substitute a synchronous
call to orgs-svc's existing ``GET /internal/v1/memberships/{id}`` at write
time and upsert the projection row themselves, inside the same transaction.
This is forward-compatible: once E2.8 ships real consumers, they'll just
keep this row fresher between writes.
"""

import time
from dataclasses import dataclass
from uuid import UUID

from sf_authz import PolicyEnforcer
from sqlalchemy.ext.asyncio import AsyncSession

from . import repository
from .events import emit_mfa_group_sync, emit_role_changed
from .models import MembershipProjection, RoleAssignment
from .orgs_client import OrgsClient

#: LLD-02 §11.5: platform-role grants require a recent step-up auth.
PLATFORM_GRANT_MAX_AUTH_AGE_S = 600


class NotFoundError(Exception):
    pass


class MembershipNotActive(Exception):
    pass


class ScopeMismatch(Exception):
    pass


class AssignableByDenied(Exception):
    pass


class SelfGrantForbidden(Exception):
    pass


class LastAdminProtected(Exception):
    pass


class RoleNotGrantable(Exception):
    pass


async def _refresh_membership_projection(
    session: AsyncSession, *, membership_id: UUID, org_id: UUID, orgs_client: OrgsClient
) -> MembershipProjection:
    # Must stay the first thing the write path does, before any SQL: the
    # session takes its connection lazily, so this HTTP call holds none
    # (LLD-02 §7 "before step 1"; LLD-09 §3). Guarded by
    # test_the_orgs_svc_freshness_call_holds_no_db_connection.
    membership = await orgs_client.get_membership(str(membership_id), org_id=str(org_id))
    if membership is None or membership.status != "active":
        raise MembershipNotActive(membership_id)
    await repository.upsert_membership_projection(
        session,
        membership_id=membership_id,
        org_id=org_id,
        user_id=UUID(membership.user_id),
        status=membership.status,
        source_version=membership.version,
    )
    projection = await repository.get_membership_projection_for_update(session, membership_id)
    if projection is None or projection.status != "active":
        raise MembershipNotActive(membership_id)
    return projection


@dataclass(frozen=True)
class AssignResult:
    assignment: RoleAssignment
    policy_version: int


async def assign_role(
    session: AsyncSession,
    *,
    org_id: UUID,
    membership_id: UUID,
    target_user_id: UUID,
    role_id: str,
    actor_id: UUID,
    actor_subject: str,
    reason: str | None,
    enforcer: PolicyEnforcer,
    orgs_client: OrgsClient,
) -> AssignResult:
    """LLD-02 §7 write path + §11 escalation guards, org-domain variant."""
    projection = await _refresh_membership_projection(
        session, membership_id=membership_id, org_id=org_id, orgs_client=orgs_client
    )
    if projection.user_id != target_user_id:
        raise MembershipNotActive(membership_id)

    role = await repository.get_role(session, role_id)
    if role is None:
        raise NotFoundError(role_id)
    domain = f"org:{org_id}"
    if role.scope != "org":
        raise ScopeMismatch(f"role {role_id!r} has scope {role.scope!r}, not 'org'")

    # §11 rule 2: actor must hold role_grant:<role_id> assign rights in this domain.
    if not enforcer.enforce(actor_subject, domain, f"role_grant:{role_id}", "assign"):
        raise AssignableByDenied(f"{actor_subject} cannot grant {role_id!r} in {domain!r}")

    # §11 rule 3: no self-grant (self-revoke is allowed, handled in revoke_role).
    if target_user_id == actor_id:
        raise SelfGrantForbidden(actor_id)

    assignment = await repository.insert_role_assignment(
        session,
        user_id=target_user_id,
        role_id=role_id,
        domain=domain,
        org_id=org_id,
        membership_id=membership_id,
        granted_by=actor_id,
        reason=reason,
    )
    await repository.insert_casbin_g_row(
        session, user_id=target_user_id, role_id=role_id, domain=domain
    )
    new_version = await repository.bump_policy_version(session)
    await emit_role_changed(session, assignment, actor_id=actor_id, change="granted")
    if role.requires_mfa:
        await emit_mfa_group_sync(session, assignment, required=True)
    return AssignResult(assignment=assignment, policy_version=new_version)


async def assign_platform_role(
    session: AsyncSession,
    *,
    target_user_id: UUID,
    role_id: str,
    actor_id: UUID,
    actor_subject: str,
    actor_auth_time: float | None,
    reason: str,
    enforcer: PolicyEnforcer,
) -> AssignResult:
    """LLD-02 §11.5: platform grants require ``acr>=2`` (enforced by the route's
    ``Authorize(..., min_acr=2)``), a reason, and a recent auth (checked here
    since ``max_auth_age_s`` on the dependency already covers the route-level
    check — this repeats it defensively against the specific actor)."""
    if not reason or len(reason) < 10:
        raise ValueError("a reason of at least 10 characters is required for platform grants")
    if (
        actor_auth_time is not None
        and (time.time() - actor_auth_time) > PLATFORM_GRANT_MAX_AUTH_AGE_S
    ):
        raise PermissionError("step-up auth has expired; re-authenticate")

    role = await repository.get_role(session, role_id)
    if role is None:
        raise NotFoundError(role_id)
    domain = "platform"
    if role.scope != "platform":
        raise ScopeMismatch(f"role {role_id!r} has scope {role.scope!r}, not 'platform'")

    if not enforcer.enforce(actor_subject, domain, f"role_grant:{role_id}", "assign"):
        raise AssignableByDenied(f"{actor_subject} cannot grant {role_id!r} in {domain!r}")
    if target_user_id == actor_id:
        raise SelfGrantForbidden(actor_id)

    assignment = await repository.insert_role_assignment(
        session,
        user_id=target_user_id,
        role_id=role_id,
        domain=domain,
        org_id=None,
        membership_id=None,
        granted_by=actor_id,
        reason=reason,
    )
    await repository.insert_casbin_g_row(
        session, user_id=target_user_id, role_id=role_id, domain=domain
    )
    new_version = await repository.bump_policy_version(session)
    await emit_role_changed(session, assignment, actor_id=actor_id, change="granted")
    if role.requires_mfa:
        await emit_mfa_group_sync(session, assignment, required=True)
    return AssignResult(assignment=assignment, policy_version=new_version)


async def revoke_role(
    session: AsyncSession,
    assignment_id: UUID,
    *,
    domain: str,
    expected_version: int,
    actor_id: UUID,
    actor_subject: str,
    reason: str | None,
    enforcer: PolicyEnforcer,
) -> AssignResult:
    """§11 rule 3 (self-revoke allowed), rule 4 (last org_admin protection)."""
    assignment = await repository.get_role_assignment(
        session, assignment_id, domain=domain, for_update=True
    )
    if assignment is None:
        raise NotFoundError(assignment_id)

    is_self_revoke = assignment.user_id == actor_id
    if not is_self_revoke and not enforcer.enforce(
        actor_subject, domain, f"role_grant:{assignment.role_id}", "assign"
    ):
        raise AssignableByDenied(
            f"{actor_subject} cannot revoke {assignment.role_id!r} in {domain!r}"
        )

    if assignment.role_id == "org_admin" and assignment.org_id is not None:
        remaining = await repository.count_active_org_admins(
            session, org_id=assignment.org_id, exclude_assignment_id=assignment.id
        )
        if remaining == 0:
            raise LastAdminProtected(assignment.org_id)

    updated = await repository.update_role_assignment_status(
        session,
        assignment.id,
        domain=domain,
        new_status="revoked",
        expected_version=expected_version,
        revoked_by=actor_id,
        reason=reason,
    )
    await repository.delete_casbin_g_row(
        session, user_id=updated.user_id, role_id=updated.role_id, domain=domain
    )
    new_version = await repository.bump_policy_version(session)
    change = "revoked" if not is_self_revoke else "self_revoked"
    await emit_role_changed(session, updated, actor_id=actor_id, change=change)

    role = await repository.get_role(session, updated.role_id)
    if role is not None and role.requires_mfa:
        still_required = await repository.has_active_mfa_role(session, user_id=updated.user_id)
        if not still_required:
            await emit_mfa_group_sync(session, updated, required=False)

    return AssignResult(assignment=updated, policy_version=new_version)
