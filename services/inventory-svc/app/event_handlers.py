"""inventory-svc's two InboxConsumer dispatch tables (LLD-08 §2.1):

- ``q.inventory.catalog`` (``product.updated.v1``): creates a zero-stock
  row for every new SKU (LLD-07 §3).
- ``q.inventory.commands`` (``inventory.*_requested.v1``): the async
  alternative to the direct internal HTTP calls in LLD-07 §4.2-§4.4, for
  whenever orders-svc exists and prefers a command over a synchronous call.

The command handlers call straight into ``service.commit``/``release``/
``restock``, which each open their *own* transaction via ``session_factory``
(so ``sf_db.retry_on_transient`` can restart them whole) rather than reusing
the inbox's own transaction -- safe only because every one of those
operations is independently idempotent (INV-4/INV-5), so a redelivery after
a partial failure just replays into a no-op.
"""

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from . import repository, service

CONSUMER_NAME_CATALOG = "inventory.catalog"
CONSUMER_NAME_COMMANDS = "inventory.commands"


async def handle_product_updated(session: AsyncSession, envelope: dict[str, Any]) -> None:
    data = envelope["data"]
    org_id = UUID(data["org_id"])
    product_id = UUID(data["product_id"])
    for sku in data.get("skus", []):
        await repository.upsert_new_stock_item(
            session, sku_id=UUID(sku["id"]), org_id=org_id, product_id=product_id
        )


CATALOG_DISPATCH = {"product.updated": handle_product_updated}


def build_command_dispatch(
    session_factory: async_sessionmaker[AsyncSession],
) -> dict[str, Any]:
    async def handle_commit_requested(_session: AsyncSession, envelope: dict[str, Any]) -> None:
        data = envelope["data"]
        await service.commit(session_factory, order_id=UUID(data["order_id"]))

    async def handle_release_requested(_session: AsyncSession, envelope: dict[str, Any]) -> None:
        data = envelope["data"]
        await service.release(
            session_factory,
            org_id=UUID(data["org_id"]),
            order_id=UUID(data["order_id"]),
            reason=data.get("reason"),
        )

    async def handle_restock_requested(_session: AsyncSession, envelope: dict[str, Any]) -> None:
        data = envelope["data"]
        lines = [(UUID(line["sku_id"]), int(line["qty"])) for line in data["lines"]]
        await service.restock(
            session_factory,
            org_id=UUID(data["org_id"]),
            order_id=UUID(data["order_id"]),
            lines=lines,
        )

    return {
        "inventory.commit_requested": handle_commit_requested,
        "inventory.release_requested": handle_release_requested,
        "inventory.restock_requested": handle_restock_requested,
    }
