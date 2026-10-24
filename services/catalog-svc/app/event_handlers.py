"""catalog-svc's InboxConsumer dispatch table (LLD-08 §2.1's ``q.catalog.orgs``
binding): the ``store_pricing_settings`` projection catalog-svc's pricing
quote reads instead of calling orgs-svc synchronously (LLD-05 §2-§3).

``sf_messaging.InboxConsumer`` already wraps the call in ``session.begin()``
and only commits + acks on a clean return, so each handler assumes it's
inside an open transaction and must not commit/rollback itself.
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from . import repository

CONSUMER_NAME = "catalog.orgs"


async def handle_organization_event(session: AsyncSession, envelope: dict) -> None:
    """Handles every ``organization.*.v1`` type with one function: they all
    carry the same projection fields (LLD-03's ``_emit_org_event``), and
    ``upsert_store_settings``'s own version guard makes applying any of
    them, in any order, converge on the org's latest state."""
    data = envelope["data"]
    await repository.upsert_store_settings(
        session,
        org_id=UUID(data["org_id"]),
        status=data["status"],
        currency=data["currency"],
        shipping_flat_minor=data["shipping_flat_minor"],
        tax_rate_bps=data["tax_rate_bps"],
        source_version=envelope["sfaggregateversion"],
    )


DISPATCH = {
    "organization.created": handle_organization_event,
    "organization.updated": handle_organization_event,
    "organization.suspended": handle_organization_event,
    "organization.reactivated": handle_organization_event,
    "organization.archived": handle_organization_event,
}
