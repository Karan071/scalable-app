"""users-svc's InboxConsumer dispatch table (``q.users.commands``, LLD-08
§4.2: ``identity.mfa_group_sync_requested``, sent by authz-svc whenever a
role with ``requires_mfa`` is granted).

The Keycloak call is the side effect LLD-08 §6 says the inbox transaction
can't cover -- it's naturally idempotent ("add to group"), so there's
nothing else to make it safe against redelivery. No DB write of its own:
this handler only reads the user's Keycloak id, so it needs no local
idempotency beyond the inbox row InboxConsumer already inserts.

Takes the ``KeycloakAdminClient`` as a parameter (``build_dispatch``)
rather than importing a module-level instance, since it's constructed in
``main.py``'s lifespan alongside the HTTP client it shares.
"""

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from . import repository
from .keycloak_admin import KeycloakAdminClient

CONSUMER_NAME = "users.commands"

MFA_REQUIRED_GROUP = "mfa-required"


def build_dispatch(
    keycloak_admin: KeycloakAdminClient,
) -> Mapping[str, Any]:
    async def handle_identity_mfa_group_sync_requested(
        session: AsyncSession, envelope: dict
    ) -> None:
        user_id = UUID(envelope["data"]["user_id"])
        user = await repository.get_by_id(session, user_id)
        if user is None:
            # Permanent: no such user, nothing to sync (LLD-08 §7).
            return
        await keycloak_admin.add_user_to_group(
            keycloak_user_id=user.idp_subject, group_name=MFA_REQUIRED_GROUP
        )

    return {
        "identity.mfa_group_sync_requested": handle_identity_mfa_group_sync_requested,
    }
