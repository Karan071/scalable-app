"""payments-svc's InboxConsumer dispatch table (LLD-08 §2.1): the three
commands orders-svc's checkout saga and order admin ops emit.

Each handler ignores the ``session`` ``InboxConsumer`` hands it and opens
its own transaction via ``session_factory`` -- same reasoning as
inventory-svc's/orders-svc's command handlers: every step here is
independently idempotent (the attempt's own status guards), so the
decoupling from the inbox's own transaction is safe.
"""

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from . import service
from .provider_client import MockProviderClient

CONSUMER_NAME = "payments.commands"


def build_command_dispatch(
    session_factory: async_sessionmaker[AsyncSession], provider: MockProviderClient
) -> dict[str, Any]:
    async def handle_capture_requested(_session: AsyncSession, envelope: dict[str, Any]) -> None:
        data = envelope["data"]
        await service.handle_capture_requested(
            session_factory, provider, order_id=UUID(data["order_id"])
        )

    async def handle_void_requested(_session: AsyncSession, envelope: dict[str, Any]) -> None:
        data = envelope["data"]
        await service.handle_void_requested(
            session_factory, provider, order_id=UUID(data["order_id"])
        )

    async def handle_refund_requested(_session: AsyncSession, envelope: dict[str, Any]) -> None:
        data = envelope["data"]
        await service.handle_refund_requested(
            session_factory, provider, order_id=UUID(data["order_id"])
        )

    return {
        "payment.capture_requested": handle_capture_requested,
        "payment.void_requested": handle_void_requested,
        "payment.refund_requested": handle_refund_requested,
    }
