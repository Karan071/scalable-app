"""Direct DB access for the ``orgs`` schema (LLD-03 §3).

List endpoints return every matching row ordered by `created_at`, not a
keyset-paginated page (LLD-09 §3's cursor pagination is an open item here —
org/member/invitation counts per tenant are small in practice for now).
"""

from datetime import UTC, datetime
from uuid import UUID, uuid7

from sf_db import violated_unique_constraint
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Invitation, Membership, Organization


class VersionConflict(Exception):
    pass


class SlugTaken(Exception):
    pass


# --- organizations -----------------------------------------------------


async def insert_organization(
    session: AsyncSession,
    *,
    slug: str,
    name: str,
    currency: str,
    shipping_flat_minor: int,
    tax_rate_bps: int,
    created_by: UUID,
) -> Organization:
    now = datetime.now(UTC)
    org = Organization(
        id=uuid7(),
        slug=slug,
        name=name,
        status="pending",
        status_reason=None,
        currency=currency,
        shipping_flat_minor=shipping_flat_minor,
        tax_rate_bps=tax_rate_bps,
        settings={},
        created_by=created_by,
        created_at=now,
        updated_at=now,
        version=1,
    )
    session.add(org)
    try:
        await session.flush()
    except IntegrityError as exc:
        if violated_unique_constraint(exc) == "uq_organizations_slug":
            raise SlugTaken(slug) from exc
        raise
    return org


async def get_organization(session: AsyncSession, org_id: UUID) -> Organization | None:
    return await session.get(Organization, org_id)


async def list_organizations(
    session: AsyncSession, *, status: str | None = None
) -> list[Organization]:
    stmt = select(Organization).order_by(Organization.created_at)
    if status is not None:
        stmt = stmt.where(Organization.status == status)
    return list((await session.execute(stmt)).scalars().all())


async def update_organization_settings(
    session: AsyncSession,
    org_id: UUID,
    *,
    expected_version: int,
    name: str | None = None,
    shipping_flat_minor: int | None = None,
    tax_rate_bps: int | None = None,
    settings: dict[str, object] | None = None,
) -> Organization:
    values: dict[str, object] = {"updated_at": datetime.now(UTC), "version": expected_version + 1}
    if name is not None:
        values["name"] = name
    if shipping_flat_minor is not None:
        values["shipping_flat_minor"] = shipping_flat_minor
    if tax_rate_bps is not None:
        values["tax_rate_bps"] = tax_rate_bps
    if settings is not None:
        values["settings"] = settings

    stmt = (
        update(Organization)
        .where(Organization.id == org_id, Organization.version == expected_version)
        .values(**values)
        .returning(Organization)
    )
    row = (await session.execute(stmt)).scalars().one_or_none()
    if row is None:
        raise VersionConflict(org_id)
    return row


async def update_organization_status(
    session: AsyncSession, org_id: UUID, *, new_status: str, reason: str | None
) -> Organization | None:
    stmt = (
        update(Organization)
        .where(Organization.id == org_id)
        .values(
            status=new_status,
            status_reason=reason,
            updated_at=datetime.now(UTC),
            version=Organization.version + 1,
        )
        .returning(Organization)
    )
    return (await session.execute(stmt)).scalars().one_or_none()


# --- memberships ---------------------------------------------------------


async def upsert_active_membership(
    session: AsyncSession, *, org_id: UUID, user_id: UUID, invited_by: UUID | None
) -> Membership:
    """Re-invite reuses the row (LLD-03 §5): same ``(org_id, user_id)``, status -> active."""
    now = datetime.now(UTC)
    stmt = (
        pg_insert(Membership)
        .values(
            id=uuid7(),
            org_id=org_id,
            user_id=user_id,
            status="active",
            status_reason=None,
            invited_by=invited_by,
            joined_at=now,
            created_at=now,
            updated_at=now,
            version=1,
        )
        .on_conflict_do_update(
            index_elements=["org_id", "user_id"],
            set_={
                "status": "active",
                "status_reason": None,
                "invited_by": invited_by,
                "joined_at": now,
                "updated_at": now,
                "version": Membership.version + 1,
            },
        )
        .returning(Membership)
    )
    row = (await session.execute(stmt)).scalars().one()
    return row


async def get_membership(
    session: AsyncSession, membership_id: UUID, *, org_id: UUID
) -> Membership | None:
    stmt = select(Membership).where(Membership.id == membership_id, Membership.org_id == org_id)
    return (await session.execute(stmt)).scalars().one_or_none()


async def get_membership_for_user(
    session: AsyncSession, *, org_id: UUID, user_id: UUID
) -> Membership | None:
    stmt = select(Membership).where(Membership.org_id == org_id, Membership.user_id == user_id)
    return (await session.execute(stmt)).scalars().one_or_none()


async def list_memberships(
    session: AsyncSession, org_id: UUID, *, status: str | None = None
) -> list[Membership]:
    stmt = select(Membership).where(Membership.org_id == org_id).order_by(Membership.created_at)
    if status is not None:
        stmt = stmt.where(Membership.status == status)
    return list((await session.execute(stmt)).scalars().all())


async def list_memberships_for_user(session: AsyncSession, user_id: UUID) -> list[Membership]:
    stmt = select(Membership).where(Membership.user_id == user_id).order_by(Membership.created_at)
    return list((await session.execute(stmt)).scalars().all())


async def disable_all_active_memberships_for_user(
    session: AsyncSession, *, user_id: UUID, reason: str
) -> list[Membership]:
    """``user.disabled`` (LLD-08 §2.1's ``q.orgs.users`` binding): every
    active membership for the user, across every org, -> disabled."""
    stmt = (
        update(Membership)
        .where(Membership.user_id == user_id, Membership.status == "active")
        .values(
            status="disabled",
            status_reason=reason,
            updated_at=datetime.now(UTC),
            version=Membership.version + 1,
        )
        .returning(Membership)
    )
    return list((await session.execute(stmt)).scalars().all())


async def update_membership_status(
    session: AsyncSession,
    membership_id: UUID,
    *,
    org_id: UUID,
    new_status: str,
    from_statuses: tuple[str, ...],
    reason: str | None = None,
) -> Membership | None:
    stmt = (
        update(Membership)
        .where(
            Membership.id == membership_id,
            Membership.org_id == org_id,
            Membership.status.in_(from_statuses),
        )
        .values(
            status=new_status,
            status_reason=reason,
            updated_at=datetime.now(UTC),
            version=Membership.version + 1,
        )
        .returning(Membership)
    )
    return (await session.execute(stmt)).scalars().one_or_none()


# --- invitations -----------------------------------------------------------


async def insert_invitation(
    session: AsyncSession,
    *,
    org_id: UUID,
    email: str,
    token_hash: bytes,
    roles_requested: list[str],
    expires_at: datetime,
    invited_by: UUID,
) -> Invitation:
    now = datetime.now(UTC)
    invitation = Invitation(
        id=uuid7(),
        org_id=org_id,
        email=email,
        token_hash=token_hash,
        roles_requested=roles_requested,
        status="pending",
        expires_at=expires_at,
        invited_by=invited_by,
        accepted_by=None,
        created_at=now,
        updated_at=now,
        version=1,
    )
    session.add(invitation)
    await session.flush()
    return invitation


async def get_invitation_by_token_hash(
    session: AsyncSession, token_hash: bytes
) -> Invitation | None:
    stmt = select(Invitation).where(Invitation.token_hash == token_hash).with_for_update()
    return (await session.execute(stmt)).scalars().one_or_none()


async def get_invitation(
    session: AsyncSession, invitation_id: UUID, *, org_id: UUID
) -> Invitation | None:
    stmt = select(Invitation).where(Invitation.id == invitation_id, Invitation.org_id == org_id)
    return (await session.execute(stmt)).scalars().one_or_none()


async def list_invitations(
    session: AsyncSession, org_id: UUID, *, status: str | None = None
) -> list[Invitation]:
    stmt = select(Invitation).where(Invitation.org_id == org_id).order_by(Invitation.created_at)
    if status is not None:
        stmt = stmt.where(Invitation.status == status)
    return list((await session.execute(stmt)).scalars().all())


async def mark_invitation_accepted(
    session: AsyncSession, invitation_id: UUID, *, accepted_by: UUID
) -> None:
    await session.execute(
        update(Invitation)
        .where(Invitation.id == invitation_id)
        .values(
            status="accepted",
            accepted_by=accepted_by,
            updated_at=datetime.now(UTC),
            version=Invitation.version + 1,
        )
    )


async def update_invitation_status(
    session: AsyncSession, invitation_id: UUID, *, org_id: UUID, new_status: str
) -> Invitation | None:
    stmt = (
        update(Invitation)
        .where(
            Invitation.id == invitation_id,
            Invitation.org_id == org_id,
            Invitation.status == "pending",
        )
        .values(status=new_status, updated_at=datetime.now(UTC), version=Invitation.version + 1)
        .returning(Invitation)
    )
    return (await session.execute(stmt)).scalars().one_or_none()


async def resend_invitation(
    session: AsyncSession,
    invitation_id: UUID,
    *,
    org_id: UUID,
    new_token_hash: bytes,
    new_expires_at: datetime,
) -> Invitation | None:
    stmt = (
        update(Invitation)
        .where(
            Invitation.id == invitation_id,
            Invitation.org_id == org_id,
            Invitation.status == "pending",
        )
        .values(
            token_hash=new_token_hash,
            expires_at=new_expires_at,
            updated_at=datetime.now(UTC),
            version=Invitation.version + 1,
        )
        .returning(Invitation)
    )
    return (await session.execute(stmt)).scalars().one_or_none()
