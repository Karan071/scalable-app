"""Direct DB access for the ``authz`` schema (LLD-02 §7)."""

from datetime import UTC, datetime
from uuid import UUID, uuid7

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from .models import CasbinRule, MembershipProjection, PolicyVersion, Role, RoleAssignment


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
