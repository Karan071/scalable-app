"""users-svc's InboxConsumer dispatch table for ``q.users.commands``
(``sf.commands`` exchange) -- a self-targeted retry, not a projection fed
by another service.

LLD-01 §11.2: "Set Keycloak attribute sf_uid (sync admin call, idempotent;
retried by the outbox command user.idp_sync_requested if it fails)." The
synchronous call happens in routes.py's provision endpoint; when it fails,
``service.request_idp_sync_retry`` queues this exact command for this
consumer to retry -- same self-consumption shape as authz-svc's
``identity.mfa_group_sync_requested`` (LLD-01 §9).

LLD-01 §11.3: disable/reactivate queue ``user.idp_status_sync_requested``
so the Keycloak user is disabled (and logged out) or re-enabled to match.
"""

import logging
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from . import repository
from .keycloak_admin import KeycloakAdminClient

logger = logging.getLogger("users.event_handlers")

CONSUMER_NAME = "users.commands"


def build_dispatch(keycloak_admin: KeycloakAdminClient) -> Mapping[str, Any]:
    async def handle_user_idp_sync_requested(session: AsyncSession, envelope: dict) -> None:
        data = envelope["data"]
        user_id = UUID(data["user_id"])
        idp_subject = data["idp_subject"]
        await keycloak_admin.set_sf_uid(keycloak_user_id=idp_subject, sf_uid=str(user_id))

    async def handle_user_idp_status_sync_requested(session: AsyncSession, envelope: dict) -> None:
        """LLD-01 §11.3: Keycloak's enabled flag follows the user's status
        as it is now, not as it was when the command was queued."""
        user = await repository.get_by_id(session, UUID(envelope["data"]["user_id"]))
        if user is None:
            return
        found = await keycloak_admin.sync_enabled(
            keycloak_user_id=user.idp_subject, enabled=user.status == "active"
        )
        if not found:
            logger.warning("no Keycloak user to sync status to", extra={"sf_uid": str(user.id)})

    return {
        "user.idp_sync_requested": handle_user_idp_sync_requested,
        "user.idp_status_sync_requested": handle_user_idp_status_sync_requested,
    }
