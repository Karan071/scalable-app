"""authz-svc's second ``InboxConsumer`` dispatch table, for ``q.authz.commands``
(``sf.commands`` exchange) -- distinct from ``event_handlers.py``'s
``q.authz.identity`` (``sf.events``, projections fed by other services).

LLD-01 §9 (accepted): granting any platform role or ``org_admin`` adds the
user to Keycloak's ``mfa-required`` group, "authz-svc -> Keycloak admin
API, from the outbox, idempotent". This is authz-svc consuming its own
outbox-published command -- the same reliability/retry machinery every
other async side effect in this codebase uses (``sf_messaging``'s
outbox/relay/inbox), rather than a synchronous call inside ``assign_role``'s
own transaction (Keycloak calls aren't transactional and shouldn't block or
fail a DB write on a downstream HTTP error).

Deliberate deviation from LLD-08 §4.2's and LLD-02 §7 step 6's wording
("handler: users") -- both still Draft v0.1, and conflict with LLD-01's
Accepted (2026-10-17) text plus the realm's own dedicated
``sf-authz-admin`` client ("used only by authz-svc to add/remove users
from the mfa-required group" -- provisioned since E2.3, unused until this).
LLD-02/LLD-08 need their own text corrected to match; that's a documentation
follow-up, not a code TODO.
"""

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from .keycloak_admin import KeycloakAdminClient

CONSUMER_NAME = "authz.commands"

MFA_REQUIRED_GROUP = "mfa-required"


def build_dispatch(keycloak_admin: KeycloakAdminClient) -> Mapping[str, Any]:
    async def handle_identity_mfa_group_sync_requested(
        session: AsyncSession, envelope: dict
    ) -> None:
        user_id = UUID(envelope["data"]["user_id"])
        keycloak_user_id = await keycloak_admin.find_keycloak_id_by_sf_uid(str(user_id))
        if keycloak_user_id is None:
            # Permanent: no such user in Keycloak, nothing to sync (LLD-08 §7).
            return
        await keycloak_admin.add_user_to_group(
            keycloak_user_id=keycloak_user_id, group_name=MFA_REQUIRED_GROUP
        )

    return {
        "identity.mfa_group_sync_requested": handle_identity_mfa_group_sync_requested,
    }
