"""cart-svc business logic (LLD-06 §2)."""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from . import repository
from .catalog_client import CatalogClient
from .models import Cart, CartItem


class SkuNotFound(Exception):
    pass


class SkuUnavailable(Exception):
    pass


class CartFull(Exception):
    pass


@dataclass(frozen=True)
class CartView:
    cart: Cart
    items: list[CartItem]


async def get_cart(session: AsyncSession, *, customer_id: UUID, org_id: UUID) -> CartView:
    cart = await repository.get_or_create_active_cart(
        session, customer_id=customer_id, org_id=org_id
    )
    items = await repository.list_items(session, cart.id)
    return CartView(cart=cart, items=items)


async def set_item(
    session: AsyncSession,
    *,
    customer_id: UUID,
    org_id: UUID,
    sku_id: UUID,
    qty: int,
    catalog: CatalogClient,
) -> CartView:
    """**Set** quantity (LLD-06 §2): idempotent and retry-safe. Validates
    the SKU against catalog-svc -- not cached yet (an open item; LLD-09's
    cache is M4 scope), so every call is a real synchronous lookup."""
    sku = await catalog.get_sku(str(org_id), str(sku_id))
    if sku is None:
        raise SkuNotFound(sku_id)
    if not sku.available:
        raise SkuUnavailable(sku_id)

    cart = await repository.get_or_create_active_cart(
        session, customer_id=customer_id, org_id=org_id
    )
    locked = await repository.lock_cart(session, cart.id, customer_id=customer_id)
    assert locked is not None  # just created/fetched above, in this transaction

    existing_lines = {item.sku_id for item in await repository.list_items(session, cart.id)}
    if sku_id not in existing_lines and len(existing_lines) >= repository.MAX_CART_LINES:
        raise CartFull(cart.id)

    await repository.upsert_item(
        session, cart_id=cart.id, sku_id=sku_id, product_id=UUID(sku.product_id), qty=qty
    )
    items = await repository.list_items(session, cart.id)
    return CartView(cart=cart, items=items)


async def remove_item(
    session: AsyncSession, *, customer_id: UUID, org_id: UUID, sku_id: UUID
) -> CartView | None:
    """Idempotent (LLD-06 §2): removing a line from a cart that doesn't
    exist, or a line that's already gone, is success either way."""
    cart = await repository.get_active_cart(session, customer_id=customer_id, org_id=org_id)
    if cart is None:
        return None
    locked = await repository.lock_cart(session, cart.id, customer_id=customer_id)
    assert locked is not None
    await repository.delete_item(session, cart_id=cart.id, sku_id=sku_id)
    items = await repository.list_items(session, cart.id)
    return CartView(cart=cart, items=items)


async def clear_cart(session: AsyncSession, *, customer_id: UUID, org_id: UUID) -> CartView | None:
    cart = await repository.get_active_cart(session, customer_id=customer_id, org_id=org_id)
    if cart is None:
        return None
    locked = await repository.lock_cart(session, cart.id, customer_id=customer_id)
    assert locked is not None
    await repository.clear_items(session, cart.id)
    return CartView(cart=cart, items=[])


async def abandon_expired_carts(session: AsyncSession) -> int:
    return await repository.mark_expired_carts_abandoned(session, now=datetime.now(UTC))
