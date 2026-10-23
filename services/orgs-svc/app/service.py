"""orgs-svc business logic (LLD-03 §4-6).

One guard LLD-03 names is a deliberately accepted gap, not a TODO:
checking the inviter actually holds `role_grant:<role>` for each requested
role (§6.1) is NOT done here, and authz-svc's `membership.created`
consumer (E2.8) does not re-check it either — it grants every role in
`roles_requested` unconditionally. Reason: an org's very first invitation
(the owner invitation, created by `create_organization` on a platform
admin's behalf) has no existing grant-holder to check against — platform
roles don't match an org domain (confirmed: `role:super_user`'s policy row
is `dom="platform"` literally, never `"*"`), so requiring a check at grant
time would make it impossible for any org to ever get its first
`org_admin`. The actual security boundary is who can create an invitation
at all (`Authorize("invitation","create",...)` + `_require_active_membership`
on `POST /v1/orgs/{org_id}/invitations`), not what roles they request in
it — an org_admin without `role_grant:inventory_manager` could still
invite someone as `inventory_manager`, a UX gap, not a security one.

The last-active-org_admin guard on disable/revoke/leave (§5) stays an open
item here for the same structural reason LLD-03 §1 names: orgs-svc has no
visibility into who holds which role at all (that's authz-svc's
`role_assignments` table — "orgs-svc owns *who is a member*; authz-svc
owns *what members may do*"). authz-svc's own `revoke_role` enforces this
guard for direct role-assignment revocation (LLD-02 §11 rule 4); orgs-svc's
membership-level operations don't have an equivalent check.
"""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sf_common import TokenCipher, get_correlation_id
from sf_messaging import add_to_outbox, build_envelope, record_audit
from sf_messaging.audit import ActorType, Severity
from sqlalchemy.ext.asyncio import AsyncSession

from . import repository
from .models import Organization
from .slugs import normalize_slug

_NOTIFY_CHANNEL = "outbox_orgs"

_ORG_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "suspend": ("pending", "active"),
    "reactivate": ("suspended",),
    "archive": ("active", "suspended"),
}


class InvalidTransition(Exception):
    pass


class VersionConflictError(Exception):
    pass


class NotFoundError(Exception):
    pass


class InvitationInvalid(Exception):
    """Covers unknown, wrong-status, and expired tokens — one response, by design (§6.2)."""


class InvitationEmailMismatch(Exception):
    pass


def _hash_token(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


async def _emit(
    session: AsyncSession,
    *,
    event_type: str,
    routing_key: str,
    aggregate_type: str,
    aggregate_id: UUID,
    aggregate_version: int,
    data: dict[str, object],
) -> None:
    envelope = build_envelope(
        event_type=event_type,
        source="orgs-svc",
        subject=str(aggregate_id),
        data=data,
        schema_version="1.0",
        aggregate_type=aggregate_type,
        aggregate_version=aggregate_version,
    )
    await add_to_outbox(
        session,
        envelope,
        exchange="sf.events",
        routing_key=routing_key,
        aggregate_id=str(aggregate_id),
        notify_channel=_NOTIFY_CHANNEL,
    )


async def audit(
    session: AsyncSession,
    *,
    action: str,
    actor_id: UUID | str,
    org_id: UUID,
    target_type: str,
    target_id: UUID,
    aggregate_version: int,
    actor_type: ActorType = "user",
    reason: str | None = None,
    changes: dict[str, object] | None = None,
    severity: Severity = "info",
) -> None:
    """LLD-04 §2.4: one ``audit.recorded`` outbox row, same transaction as
    the audited change. Every orgs-svc action is scoped to its org's
    domain, so org admins see them through the org-scoped audit query."""
    await record_audit(
        session,
        notify_channel=_NOTIFY_CHANNEL,
        source_service="orgs-svc",
        action=action,
        actor_type=actor_type,
        actor_id=str(actor_id),
        domain=f"org:{org_id}",
        target_type=target_type,
        target_id=str(target_id),
        aggregate_version=aggregate_version,
        reason=reason,
        changes=changes,
        severity=severity,
        correlation_id=get_correlation_id(),
    )


async def _emit_org_event(
    session: AsyncSession, org: Organization, event_type: str, routing_key: str
) -> None:
    await _emit(
        session,
        event_type=event_type,
        routing_key=routing_key,
        aggregate_type="organization",
        aggregate_id=org.id,
        aggregate_version=org.version,
        data={"org_id": str(org.id), "slug": org.slug, "status": org.status},
    )


# --- organizations -----------------------------------------------------------


@dataclass(frozen=True)
class CreatedOrganization:
    organization: Organization
    owner_invitation: "CreatedInvitation"


async def create_organization(
    session: AsyncSession,
    *,
    slug: str,
    name: str,
    currency: str,
    shipping_flat_minor: int,
    tax_rate_bps: int,
    created_by: UUID,
    owner_email: str,
    ttl_days: int,
    cipher: TokenCipher,
) -> CreatedOrganization:
    """Creates a ``pending`` org plus its owner invitation (LLD-03 §4): the
    org has no members at all until that invitation is accepted, which is
    what moves it to ``active`` (see ``accept_invitation``)."""
    normalized_slug = normalize_slug(slug)
    org = await repository.insert_organization(
        session,
        slug=normalized_slug,
        name=name,
        currency=currency,
        shipping_flat_minor=shipping_flat_minor,
        tax_rate_bps=tax_rate_bps,
        created_by=created_by,
    )
    await _emit_org_event(session, org, "organization.created", "organization.created.v1")
    await audit(
        session,
        action="org.created",
        actor_id=created_by,
        org_id=org.id,
        target_type="organization",
        target_id=org.id,
        aggregate_version=org.version,
        changes={
            "name": {"to": org.name},
            "slug": {"to": org.slug},
            "status": {"to": org.status},
        },
    )

    owner_invitation = await create_invitation(
        session,
        org_id=org.id,
        email=owner_email,
        roles_requested=["org_admin"],
        invited_by=created_by,
        ttl_days=ttl_days,
        cipher=cipher,
    )
    return CreatedOrganization(organization=org, owner_invitation=owner_invitation)


async def update_organization_settings(
    session: AsyncSession,
    org_id: UUID,
    *,
    expected_version: int,
    actor_id: UUID,
    name: str | None = None,
    shipping_flat_minor: int | None = None,
    tax_rate_bps: int | None = None,
    settings: dict[str, object] | None = None,
) -> Organization:
    try:
        org = await repository.update_organization_settings(
            session,
            org_id,
            expected_version=expected_version,
            name=name,
            shipping_flat_minor=shipping_flat_minor,
            tax_rate_bps=tax_rate_bps,
            settings=settings,
        )
    except repository.VersionConflict as exc:
        raise VersionConflictError(org_id) from exc
    await _emit_org_event(session, org, "organization.updated", "organization.updated.v1")
    # LLD-04 §2.7 allow-list for org.updated. The free-form `settings` map
    # is deliberately excluded: only its presence is recorded, not content.
    changed: dict[str, object] = {
        field: {"to": value}
        for field, value in (
            ("name", name),
            ("shipping_flat_minor", shipping_flat_minor),
            ("tax_rate_bps", tax_rate_bps),
        )
        if value is not None
    }
    if settings is not None:
        changed["settings"] = {"to": "<updated>"}
    await audit(
        session,
        action="org.updated",
        actor_id=actor_id,
        org_id=org.id,
        target_type="organization",
        target_id=org.id,
        aggregate_version=org.version,
        changes=changed,
    )
    return org


async def transition_organization(
    session: AsyncSession, org_id: UUID, *, action: str, reason: str | None, actor_id: UUID
) -> Organization:
    """``action`` is one of ``suspend``/``reactivate``/``archive`` (LLD-03 §4)."""
    org = await repository.get_organization(session, org_id)
    if org is None:
        raise NotFoundError(org_id)
    if org.status not in _ORG_TRANSITIONS[action]:
        raise InvalidTransition(f"cannot {action} an org in status {org.status!r}")

    new_status = {"suspend": "suspended", "reactivate": "active", "archive": "archived"}[action]
    # Read before the UPDATE: its ORM RETURNING refreshes `org` in place.
    previous_status = org.status
    updated = await repository.update_organization_status(
        session, org_id, new_status=new_status, reason=reason
    )
    if updated is None:
        raise NotFoundError(org_id)
    # The event name is the *transition* (LLD-08 §4.1, LLD-02 §9: "suspended"
    # / "reactivated" / "archived"), not the resulting status column value —
    # "reactivate" lands the org back in status "active", but the event is
    # "organization.reactivated", never "organization.active" (that string
    # isn't in any event catalog and authz-svc's consumer wouldn't dispatch
    # on it).
    event_name = {"suspend": "suspended", "reactivate": "reactivated", "archive": "archived"}[
        action
    ]
    await _emit_org_event(
        session, updated, f"organization.{event_name}", f"organization.{event_name}.v1"
    )
    await audit(
        session,
        action=f"org.{event_name}",
        actor_id=actor_id,
        org_id=updated.id,
        target_type="organization",
        target_id=updated.id,
        aggregate_version=updated.version,
        reason=reason,
        changes={"status": {"from": previous_status, "to": updated.status}},
        severity="notice",
    )
    return updated


# --- invitations ---------------------------------------------------------


@dataclass(frozen=True)
class CreatedInvitation:
    invitation_id: UUID
    raw_token: str
    expires_at: datetime


async def create_invitation(
    session: AsyncSession,
    *,
    org_id: UUID,
    email: str,
    roles_requested: list[str],
    invited_by: UUID,
    ttl_days: int,
    cipher: TokenCipher,
) -> CreatedInvitation:
    raw_token = secrets.token_urlsafe(32)
    expires_at = datetime.now(UTC) + timedelta(days=ttl_days)
    invitation = await repository.insert_invitation(
        session,
        org_id=org_id,
        email=email,
        token_hash=_hash_token(raw_token),
        roles_requested=roles_requested,
        expires_at=expires_at,
        invited_by=invited_by,
    )
    token_enc = cipher.encrypt(raw_token, aad=str(invitation.id).encode())
    await _emit(
        session,
        event_type="invitation.created",
        routing_key="invitation.created.v1",
        aggregate_type="invitation",
        aggregate_id=invitation.id,
        aggregate_version=invitation.version,
        data={
            "invitation_id": str(invitation.id),
            "org_id": str(org_id),
            "email": email,
            "expires_at": expires_at.isoformat(),
            "token_enc": token_enc,
        },
    )
    # Never the token (LLD-04 §2.7), nor the invitee's email: the
    # invitation row is the target, and it holds the email for anyone
    # authorized to look it up.
    await audit(
        session,
        action="invitation.created",
        actor_id=invited_by,
        org_id=org_id,
        target_type="invitation",
        target_id=invitation.id,
        aggregate_version=invitation.version,
        changes={"roles_requested": {"to": roles_requested}},
    )
    return CreatedInvitation(
        invitation_id=invitation.id, raw_token=raw_token, expires_at=expires_at
    )


async def resend_invitation(
    session: AsyncSession,
    invitation_id: UUID,
    *,
    org_id: UUID,
    actor_id: UUID,
    ttl_days: int,
    cipher: TokenCipher,
) -> CreatedInvitation:
    raw_token = secrets.token_urlsafe(32)
    expires_at = datetime.now(UTC) + timedelta(days=ttl_days)
    invitation = await repository.resend_invitation(
        session,
        invitation_id,
        org_id=org_id,
        new_token_hash=_hash_token(raw_token),
        new_expires_at=expires_at,
    )
    if invitation is None:
        raise NotFoundError(invitation_id)

    token_enc = cipher.encrypt(raw_token, aad=str(invitation.id).encode())
    await _emit(
        session,
        event_type="invitation.created",
        routing_key="invitation.created.v1",
        aggregate_type="invitation",
        aggregate_id=invitation.id,
        aggregate_version=invitation.version,
        data={
            "invitation_id": str(invitation.id),
            "org_id": str(org_id),
            "email": invitation.email,
            "expires_at": expires_at.isoformat(),
            "token_enc": token_enc,
        },
    )
    await audit(
        session,
        action="invitation.resent",
        actor_id=actor_id,
        org_id=org_id,
        target_type="invitation",
        target_id=invitation.id,
        aggregate_version=invitation.version,
    )
    return CreatedInvitation(
        invitation_id=invitation.id, raw_token=raw_token, expires_at=expires_at
    )


async def revoke_invitation(
    session: AsyncSession, invitation_id: UUID, *, org_id: UUID, actor_id: UUID
) -> None:
    invitation = await repository.update_invitation_status(
        session, invitation_id, org_id=org_id, new_status="revoked"
    )
    if invitation is None:
        raise NotFoundError(invitation_id)
    await _emit(
        session,
        event_type="invitation.revoked",
        routing_key="invitation.revoked.v1",
        aggregate_type="invitation",
        aggregate_id=invitation.id,
        aggregate_version=invitation.version,
        data={"invitation_id": str(invitation.id), "org_id": str(org_id)},
    )
    await audit(
        session,
        action="invitation.revoked",
        actor_id=actor_id,
        org_id=org_id,
        target_type="invitation",
        target_id=invitation.id,
        aggregate_version=invitation.version,
    )


@dataclass(frozen=True)
class AcceptedInvitation:
    org_id: UUID
    membership_id: UUID


async def accept_invitation(
    session: AsyncSession,
    *,
    token: str,
    user_id: UUID,
    user_email: str,
    user_email_verified: bool,
) -> AcceptedInvitation:
    invitation = await repository.get_invitation_by_token_hash(session, _hash_token(token))
    if invitation is None or invitation.status != "pending":
        raise InvitationInvalid()
    if invitation.expires_at <= datetime.now(UTC):
        await repository.update_invitation_status(
            session, invitation.id, org_id=invitation.org_id, new_status="expired"
        )
        await _emit(
            session,
            event_type="invitation.expired",
            routing_key="invitation.expired.v1",
            aggregate_type="invitation",
            aggregate_id=invitation.id,
            aggregate_version=invitation.version,
            data={"invitation_id": str(invitation.id), "org_id": str(invitation.org_id)},
        )
        raise InvitationInvalid()

    if not user_email_verified or user_email.casefold() != invitation.email.casefold():
        raise InvitationEmailMismatch()

    membership = await repository.upsert_active_membership(
        session, org_id=invitation.org_id, user_id=user_id, invited_by=invitation.invited_by
    )
    await repository.mark_invitation_accepted(session, invitation.id, accepted_by=user_id)

    org = await repository.get_organization(session, invitation.org_id)
    if org is not None and org.status == "pending":
        # The org has no members until its owner invitation is accepted
        # (LLD-03 §4): that's exactly this transition.
        activated = await repository.update_organization_status(
            session, org.id, new_status="active", reason=None
        )
        if activated is not None:
            await _emit_org_event(
                session, activated, "organization.updated", "organization.updated.v1"
            )
            await audit(
                session,
                action="org.updated",
                actor_id=user_id,
                org_id=activated.id,
                target_type="organization",
                target_id=activated.id,
                aggregate_version=activated.version,
                reason="owner_invitation_accepted",
                changes={"status": {"from": "pending", "to": activated.status}},
            )

    await _emit(
        session,
        event_type="membership.created",
        routing_key="membership.created.v1",
        aggregate_type="membership",
        aggregate_id=membership.id,
        aggregate_version=membership.version,
        data={
            "membership_id": str(membership.id),
            "org_id": str(invitation.org_id),
            "user_id": str(user_id),
            "roles_requested": invitation.roles_requested,
            "invited_by": str(invitation.invited_by),
        },
    )
    await _emit(
        session,
        event_type="invitation.accepted",
        routing_key="invitation.accepted.v1",
        aggregate_type="invitation",
        aggregate_id=invitation.id,
        aggregate_version=invitation.version,
        data={"invitation_id": str(invitation.id), "org_id": str(invitation.org_id)},
    )
    await audit(
        session,
        action="invitation.accepted",
        actor_id=user_id,
        org_id=invitation.org_id,
        target_type="invitation",
        target_id=invitation.id,
        aggregate_version=invitation.version,
    )
    await audit(
        session,
        action="membership.created",
        actor_id=user_id,
        org_id=invitation.org_id,
        target_type="membership",
        target_id=membership.id,
        aggregate_version=membership.version,
        changes={
            "user_id": {"to": str(user_id)},
            "roles_requested": {"to": invitation.roles_requested},
        },
    )
    return AcceptedInvitation(org_id=invitation.org_id, membership_id=membership.id)


# --- memberships -----------------------------------------------------------


async def _transition_membership(
    session: AsyncSession,
    membership_id: UUID,
    *,
    org_id: UUID,
    new_status: str,
    from_statuses: tuple[str, ...],
    reason: str | None,
    event_type: str,
    routing_key: str,
    actor_id: UUID,
) -> None:
    membership = await repository.update_membership_status(
        session,
        membership_id,
        org_id=org_id,
        new_status=new_status,
        from_statuses=from_statuses,
        reason=reason,
    )
    if membership is None:
        raise NotFoundError(membership_id)
    await _emit(
        session,
        event_type=event_type,
        routing_key=routing_key,
        aggregate_type="membership",
        aggregate_id=membership.id,
        aggregate_version=membership.version,
        data={
            "membership_id": str(membership.id),
            "org_id": str(org_id),
            "user_id": str(membership.user_id),
            "actor_id": str(actor_id),
        },
    )
    await audit(
        session,
        action=event_type,
        actor_id=actor_id,
        org_id=org_id,
        target_type="membership",
        target_id=membership.id,
        aggregate_version=membership.version,
        reason=reason,
        changes={
            "user_id": {"to": str(membership.user_id)},
            "status": {"to": membership.status},
        },
        severity="notice",
    )


async def disable_membership(
    session: AsyncSession, membership_id: UUID, *, org_id: UUID, actor_id: UUID, reason: str | None
) -> None:
    await _transition_membership(
        session,
        membership_id,
        org_id=org_id,
        new_status="disabled",
        from_statuses=("active",),
        reason=reason,
        event_type="membership.disabled",
        routing_key="membership.disabled.v1",
        actor_id=actor_id,
    )


async def reactivate_membership(
    session: AsyncSession, membership_id: UUID, *, org_id: UUID, actor_id: UUID
) -> None:
    await _transition_membership(
        session,
        membership_id,
        org_id=org_id,
        new_status="active",
        from_statuses=("disabled",),
        reason=None,
        event_type="membership.reactivated",
        routing_key="membership.reactivated.v1",
        actor_id=actor_id,
    )


async def revoke_membership(
    session: AsyncSession, membership_id: UUID, *, org_id: UUID, actor_id: UUID, reason: str | None
) -> None:
    await _transition_membership(
        session,
        membership_id,
        org_id=org_id,
        new_status="revoked",
        from_statuses=("active", "disabled"),
        reason=reason,
        event_type="membership.revoked",
        routing_key="membership.revoked.v1",
        actor_id=actor_id,
    )
