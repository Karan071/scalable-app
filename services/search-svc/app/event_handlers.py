"""search-svc's InboxConsumer dispatch table (LLD-08 §2.1's ``q.search.catalog``
binding; LLD-05 §7.2): indexing catalog-svc's product lifecycle.

``sf_messaging.InboxConsumer`` already wraps the call in ``session.begin()``
and only commits + acks on a clean return, so this handler assumes it's
inside an open transaction and must not commit/rollback itself.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from . import service

CONSUMER_NAME = "search.catalog"


async def handle_product_event(session: AsyncSession, envelope: dict) -> None:
    await service.apply_product_event(session, envelope)


DISPATCH = {
    "product.updated": handle_product_event,
    "product.deleted": handle_product_event,
}
