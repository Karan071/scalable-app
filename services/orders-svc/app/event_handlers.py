"""orders-svc's InboxConsumer dispatch tables (LLD-08 §2.1).

``q.orders.inventory`` (real today -- inventory-svc exists and really
publishes these): the async continuation past ``COMMITTING`` (LLD-06
§5.1 steps 14-15).

``q.orders.payments`` (not real yet -- payments-svc doesn't exist, E3.6):
written against LLD-06 §7's documented events so this is the one place
that changes when it lands. Declared now so the saga's async tail is
complete and independently testable via ``event_handlers`` directly.

Each handler ignores the ``session`` ``InboxConsumer`` hands it and opens
its own transaction via ``session_factory`` -- the same reasoning as
inventory-svc's command handlers (sf_messaging/consumer.py's docstring):
every step here is independently idempotent (the saga's own guarded
transitions), so the slight decoupling from the inbox's own transaction
is safe.
"""

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from . import clients, service

CONSUMER_NAME_INVENTORY = "orders.inventory"
CONSUMER_NAME_PAYMENTS = "orders.payments"


def build_inventory_dispatch(
    session_factory: async_sessionmaker[AsyncSession],
) -> dict[str, Any]:
    async def handle_committed(_session: AsyncSession, envelope: dict[str, Any]) -> None:
        data = envelope["data"]
        await service.on_inventory_committed(session_factory, order_id=UUID(data["order_id"]))

    async def handle_reservation_expired(_session: AsyncSession, envelope: dict[str, Any]) -> None:
        data = envelope["data"]
        await service.on_inventory_reservation_expired(
            session_factory, order_id=UUID(data["order_id"])
        )

    return {
        "inventory.committed": handle_committed,
        "inventory.reservation_expired": handle_reservation_expired,
    }


def build_payments_dispatch(
    session_factory: async_sessionmaker[AsyncSession], inventory_client: clients.InventoryClient
) -> dict[str, Any]:
    async def handle_authorized(_session: AsyncSession, envelope: dict[str, Any]) -> None:
        data = envelope["data"]
        await service.on_payment_authorized(
            session_factory, order_id=UUID(data["order_id"]), payment_id=UUID(data["payment_id"])
        )

    async def handle_failed(_session: AsyncSession, envelope: dict[str, Any]) -> None:
        data = envelope["data"]
        await service.on_payment_failed(
            session_factory,
            inventory_client,
            order_id=UUID(data["order_id"]),
            org_id=UUID(data["org_id"]),
        )

    async def handle_succeeded(_session: AsyncSession, envelope: dict[str, Any]) -> None:
        data = envelope["data"]
        await service.on_payment_succeeded(
            session_factory, order_id=UUID(data["order_id"]), payment_id=UUID(data["payment_id"])
        )

    return {
        "payment.authorized": handle_authorized,
        "payment.failed": handle_failed,
        "payment.succeeded": handle_succeeded,
    }
