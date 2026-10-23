"""Direct DB access for the ``authz`` schema (LLD-02 §7)."""

from datetime import UTC, datetime
from uuid import UUID, uuid7

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from .models import (
    CasbinRule,
    DomainStatus,
    MembershipProjection,
    PolicyVersion,
    Role,
    RoleAssignment,
)


class VersionConflict(Exception):
    pass


# --- roles -----------------------------------------------------------------


async def get_role(session: AsyncSession, role_id: str) -> Role | None:
    return await session.get(Role, role_id)


async def list_roles(session: AsyncSession, *, scope: str | None = None) -> list[Role]:
    stmt = select(Role).order_by(Role.id)
    if scope is not None:
        stmt = stmt.where(Role.scope == scope)
    return list((await session.execute(stmt)).scalars().all())


# --- role assignments --------------------------------------------------------


async def insert_role_assignment(
    session: AsyncSession,
    *,
    user_id: UUID,
    role_id: str,
    domain: str,
    org_id: UUID | None,
    membership_id: UUID | None,
    granted_by: UUID,
    reason: str | None,
) -> RoleAssignment:
    now = datetime.now(UTC)
    assignment = RoleAssignment(
        id=uuid7(),
        user_id=user_id,
        role_id=role_id,
        domain=domain,
        org_id=org_id,
        membership_id=membership_id,
        status="active",
        granted_by=granted_by,
        granted_at=now,
        revoked_by=None,
        revoked_at=None,
        reason=reason,
        version=1,
    )
    session.add(assignment)
    await session.flush()
    return assignment


async def get_role_assignment(
    session: AsyncSession, assignment_id: UUID, *, domain: str, for_update: bool = False
) -> RoleAssignment | None:
    stmt = select(RoleAssignment).where(
        RoleAssignment.id == assignment_id, RoleAssignment.domain == domain
    )
    if for_update:
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalars().one_or_none()


async def list_role_assignments(
    session: AsyncSession, *, domain: str, user_id: UUID | None = None
) -> list[RoleAssignment]:
    stmt = select(RoleAssignment).where(RoleAssignment.domain == domain)
    if user_id is not None:
        stmt = stmt.where(RoleAssignment.user_id == user_id)
    return list((await session.execute(stmt.order_by(RoleAssignment.granted_at))).scalars().all())


async def list_active_role_assignments_for_user(
    session: AsyncSession, user_id: UUID
) -> list[RoleAssignment]:
    """Every domain (org:* and platform) at once — backs the self-scoped
    ``GET /v1/me/role-assignments`` (E2.10), used for session-bootstrap's
    ``memberships[].roles[]``/``platform_roles[]``. No domain filter, so no
    ``Authorize(...)`` beyond authentication: reading your own grants isn't
    a privilege question."""
    stmt = (
        select(RoleAssignment)
        .where(RoleAssignment.user_id == user_id, RoleAssignment.status == "active")
        .order_by(RoleAssignment.granted_at)
    )
    return list((await session.execute(stmt)).scalars().all())


async def update_role_assignment_status(
    session: AsyncSession,
    assignment_id: UUID,
    *,
    domain: str,
    new_status: str,
    expected_version: int,
    revoked_by: UUID | None,
    reason: str | None,
) -> RoleAssignment:
    values: dict[str, object] = {
        "status": new_status,
        "version": expected_version + 1,
    }
    if new_status == "revoked":
        values["revoked_by"] = revoked_by
        values["revoked_at"] = datetime.now(UTC)
        values["reason"] = reason
    stmt = (
        update(RoleAssignment)
        .where(
            RoleAssignment.id == assignment_id,
            RoleAssignment.domain == domain,
            RoleAssignment.version == expected_version,
        )
        .values(**values)
        .returning(RoleAssignment)
    )
    row = (await session.execute(stmt)).scalars().one_or_none()
    if row is None:
        raise VersionConflict(assignment_id)
    return row


async def count_active_org_admins(
    session: AsyncSession, *, org_id: UUID, exclude_assignment_id: UUID | None = None
) -> int:
    """Locks the matching rows (LLD-02 §11.4's last-org_admin guard).

    Postgres rejects ``FOR UPDATE`` combined with an aggregate, so the rows
    are locked with a plain ``SELECT ... FOR UPDATE`` first, then counted.
    """
    conditions = (
        RoleAssignment.domain == f"org:{org_id}",
        RoleAssignment.role_id == "org_admin",
        RoleAssignment.status == "active",
    )
    lock_stmt = select(RoleAssignment.id).where(*conditions).with_for_update()
    count_stmt = select(func.count()).select_from(RoleAssignment).where(*conditions)
    if exclude_assignment_id is not None:
        lock_stmt = lock_stmt.where(RoleAssignment.id != exclude_assignment_id)
        count_stmt = count_stmt.where(RoleAssignment.id != exclude_assignment_id)

    await session.execute(lock_stmt)
    return (await session.execute(count_stmt)).scalar_one()


async def count_active_assignments_for_role(session: AsyncSession, role_id: str) -> int:
    """Used by the bootstrap CLI (§11.6) to refuse if a super_user already exists.

    Postgres rejects ``FOR UPDATE`` combined with an aggregate, so the rows
    are locked with a plain ``SELECT ... FOR UPDATE`` first, then counted.
    """
    conditions = (RoleAssignment.role_id == role_id, RoleAssignment.status == "active")
    await session.execute(select(RoleAssignment.id).where(*conditions).with_for_update())
    count_stmt = select(func.count()).select_from(RoleAssignment).where(*conditions)
    return (await session.execute(count_stmt)).scalar_one()


async def has_active_mfa_role(session: AsyncSession, *, user_id: UUID) -> bool:
    """True if the user holds any active role with ``requires_mfa`` (any
    domain). Used after a revoke to decide whether they should also come
    out of Keycloak's ``mfa-required`` group (LLD-01 §9) -- called only
    after the revoked row's own status is already updated to
    ``'revoked'``, so it naturally excludes itself without needing to be
    told which assignment just changed."""
    stmt = (
        select(func.count())
        .select_from(RoleAssignment)
        .join(Role, Role.id == RoleAssignment.role_id)
        .where(
            RoleAssignment.user_id == user_id,
            RoleAssignment.status == "active",
            Role.requires_mfa.is_(True),
        )
    )
    return (await session.execute(stmt)).scalar_one() > 0


# --- casbin_rule (g-rows only; p-rows are seeded by migration) --------------


async def insert_casbin_g_row(
    session: AsyncSession, *, user_id: UUID, role_id: str, domain: str
) -> None:
    session.add(
        CasbinRule(
            ptype="g",
            v0=f"user:{user_id}",
            v1=f"role:{role_id}",
            v2=domain,
            v3=None,
            v4=None,
            v5=None,
        )
    )
    await session.flush()


async def delete_casbin_g_row(
    session: AsyncSession, *, user_id: UUID, role_id: str, domain: str
) -> None:
    stmt = select(CasbinRule).where(
        CasbinRule.ptype == "g",
        CasbinRule.v0 == f"user:{user_id}",
        CasbinRule.v1 == f"role:{role_id}",
        CasbinRule.v2 == domain,
    )
    rows = (await session.execute(stmt)).scalars().all()
    for row in rows:
        await session.delete(row)
    await session.flush()


async def bump_policy_version(session: AsyncSession) -> int:
    stmt = (
        update(PolicyVersion)
        .where(PolicyVersion.id == 1)
        .values(version=PolicyVersion.version + 1, updated_at=datetime.now(UTC))
        .returning(PolicyVersion.version)
    )
    return (await session.execute(stmt)).scalar_one()


# --- membership projection (interim synchronous substitute for E2.8) -------


async def upsert_membership_projection(
    session: AsyncSession,
    *,
    membership_id: UUID,
    org_id: UUID,
    user_id: UUID,
    status: str,
    source_version: int,
) -> None:
    stmt = (
        pg_insert(MembershipProjection)
        .values(
            membership_id=membership_id,
            org_id=org_id,
            user_id=user_id,
            status=status,
            source_version=source_version,
        )
        .on_conflict_do_update(
            index_elements=["membership_id"],
            set_={"status": status, "source_version": source_version},
            where=MembershipProjection.source_version <= source_version,
        )
    )
    await session.execute(stmt)


async def get_membership_projection_for_update(
    session: AsyncSession, membership_id: UUID
) -> MembershipProjection | None:
    stmt = (
        select(MembershipProjection)
        .where(MembershipProjection.membership_id == membership_id)
        .with_for_update()
    )
    return (await session.execute(stmt)).scalars().one_or_none()


# --- event-driven writes (LLD-02 §9, via sf_messaging.InboxConsumer) --------


async def grant_role_unconditional(
    session: AsyncSession,
    *,
    user_id: UUID,
    role_id: str,
    domain: str,
    org_id: UUID | None,
    membership_id: UUID | None,
    granted_by: UUID,
    reason: str | None,
) -> RoleAssignment | None:
    """Used by ``membership.created``'s bulk grant and ``user.created``'s
    customer grant — unconditional (no ``role_grant`` check; see LLD-02 §9
    and the design note in ``service.py``'s module docstring for why).
    Returns ``None`` (no-op) if an active/suspended row already exists for
    this (user, role, domain) — defensive idempotency layered on top of the
    inbox table's own dedup, in case a handler is ever invoked twice outside
    of a single message delivery.
    """
    existing = await session.execute(
        select(RoleAssignment.id).where(
            RoleAssignment.user_id == user_id,
            RoleAssignment.role_id == role_id,
            RoleAssignment.domain == domain,
            RoleAssignment.status != "revoked",
        )
    )
    if existing.first() is not None:
        return None
    assignment = await insert_role_assignment(
        session,
        user_id=user_id,
        role_id=role_id,
        domain=domain,
        org_id=org_id,
        membership_id=membership_id,
        granted_by=granted_by,
        reason=reason,
    )
    await insert_casbin_g_row(session, user_id=user_id, role_id=role_id, domain=domain)
    return assignment


async def suspend_role_assignments_for_membership(
    session: AsyncSession, *, user_id: UUID, org_id: UUID, reason: str
) -> list[RoleAssignment]:
    """``membership.disabled``: active org-domain assignments -> suspended,
    tagging ``reason`` so a later targeted reactivation knows which ones it
    caused (reusing the ``reason`` column the same way revoke already
    overwrites it on status transitions)."""
    stmt = (
        update(RoleAssignment)
        .where(
            RoleAssignment.user_id == user_id,
            RoleAssignment.domain == f"org:{org_id}",
            RoleAssignment.status == "active",
        )
        .values(status="suspended", reason=reason, version=RoleAssignment.version + 1)
        .returning(RoleAssignment)
    )
    return list((await session.execute(stmt)).scalars().all())


async def reactivate_role_assignments_suspended_for_reason(
    session: AsyncSession,
    *,
    user_id: UUID,
    org_id: UUID | None,
    reason: str,
    role_id: str | None = None,
) -> list[RoleAssignment]:
    """Only rows suspended *for this specific reason* (optionally further
    narrowed to one ``role_id``) flip back to active — e.g.
    ``membership.reactivated`` must not resurrect a role ``user.disabled``
    suspended, and vice versa."""
    conditions = [
        RoleAssignment.user_id == user_id,
        RoleAssignment.status == "suspended",
        RoleAssignment.reason == reason,
    ]
    if org_id is not None:
        conditions.append(RoleAssignment.domain == f"org:{org_id}")
    if role_id is not None:
        conditions.append(RoleAssignment.role_id == role_id)
    stmt = (
        update(RoleAssignment)
        .where(*conditions)
        .values(status="active", reason=None, version=RoleAssignment.version + 1)
        .returning(RoleAssignment)
    )
    return list((await session.execute(stmt)).scalars().all())


async def revoke_role_assignments_for_membership(
    session: AsyncSession, *, user_id: UUID, org_id: UUID, reason: str
) -> list[RoleAssignment]:
    """``membership.revoked``: active or suspended org-domain assignments -> revoked."""
    stmt = (
        update(RoleAssignment)
        .where(
            RoleAssignment.user_id == user_id,
            RoleAssignment.domain == f"org:{org_id}",
            RoleAssignment.status.in_(("active", "suspended")),
        )
        .values(
            status="revoked",
            revoked_at=datetime.now(UTC),
            reason=reason,
            version=RoleAssignment.version + 1,
        )
        .returning(RoleAssignment)
    )
    return list((await session.execute(stmt)).scalars().all())


async def suspend_all_role_assignments_for_user(
    session: AsyncSession, *, user_id: UUID, reason: str
) -> list[RoleAssignment]:
    """``user.disabled``: ALL active assignments for the user, across every
    domain, -> suspended. Kept distinct from the org-scoped suspend function
    above since a user can hold roles in many domains at once — the handler
    must delete each returned row's own ``g``-row by its own ``.domain``,
    not assume one shared domain."""
    stmt = (
        update(RoleAssignment)
        .where(RoleAssignment.user_id == user_id, RoleAssignment.status == "active")
        .values(status="suspended", reason=reason, version=RoleAssignment.version + 1)
        .returning(RoleAssignment)
    )
    return list((await session.execute(stmt)).scalars().all())


async def upsert_domain_status(
    session: AsyncSession, *, domain: str, status: str, source_version: int
) -> DomainStatus | None:
    """Apply-only-if-newer (LLD-08 §9's out-of-order projection rule).
    Returns ``None`` if the existing row's ``source_version`` was already
    >= the incoming one (stale/duplicate event, correctly a no-op)."""
    stmt = (
        pg_insert(DomainStatus)
        .values(
            domain=domain,
            status=status,
            updated_at=datetime.now(UTC),
            source_version=source_version,
        )
        .on_conflict_do_update(
            index_elements=["domain"],
            set_={
                "status": status,
                "updated_at": datetime.now(UTC),
                "source_version": source_version,
            },
            where=DomainStatus.source_version < source_version,
        )
        .returning(DomainStatus)
    )
    return (await session.execute(stmt)).scalars().one_or_none()
