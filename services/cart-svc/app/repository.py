"""cart-svc data access (LLD-06 §2)."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid7

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Cart, CartItem

#: LLD-06 §2: "expires_at (30 days of inactivity)".
_CART_TTL_DAYS = 30
#: LLD-06 §2: "At most 50 lines per cart".
MAX_CART_LINES = 50


async def get_active_cart(session: AsyncSession, *, customer_id: UUID, org_id: UUID) -> Cart | None:
    stmt = select(Cart).where(
        Cart.customer_id == customer_id, Cart.org_id == org_id, Cart.status == "active"
    )
    return (await session.execute(stmt)).scalars().one_or_none()


async def get_or_create_active_cart(
    session: AsyncSession, *, customer_id: UUID, org_id: UUID
) -> Cart:
    """Lazy-create (LLD-06 §2's GET /v1/me/carts/{orgId}). The partial unique
    index on (customer_id, org_id) WHERE status='active' is the real race
    guard: ``ON CONFLICT DO NOTHING`` plus a re-select handles two concurrent
    first requests safely, without needing an advisory lock."""
    existing = await get_active_cart(session, customer_id=customer_id, org_id=org_id)
    if existing is not None:
        return existing

    now = datetime.now(UTC)
    stmt = (
        pg_insert(Cart)
        .values(
            id=uuid7(),
            customer_id=customer_id,
            org_id=org_id,
            status="active",
            version=1,
            created_at=now,
            updated_at=now,
            expires_at=now + timedelta(days=_CART_TTL_DAYS),
        )
        .on_conflict_do_nothing(
            index_elements=["customer_id", "org_id"], index_where=Cart.status == "active"
        )
    )
    await session.execute(stmt)
    created = await get_active_cart(session, customer_id=customer_id, org_id=org_id)
    assert created is not None  # either this insert or a concurrent one landed
    return created


async def lock_cart(session: AsyncSession, cart_id: UUID, *, customer_id: UUID) -> Cart | None:
    """Serializes concurrent writes to the same cart (add/remove/clear) with
    a row lock -- simpler and sufficient for a customer's own cart, unlike
    the optimistic ``version`` column (which exists for checkout's
    ``cart_version`` snapshot check, not for every item mutation)."""
    stmt = (
        select(Cart)
        .where(Cart.id == cart_id, Cart.customer_id == customer_id, Cart.status == "active")
        .with_for_update()
    )
    return (await session.execute(stmt)).scalars().one_or_none()


async def touch_cart(session: AsyncSession, cart_id: UUID) -> None:
    cart = await session.get(Cart, cart_id)
    assert cart is not None
    cart.version += 1
    cart.updated_at = datetime.now(UTC)


async def list_items(session: AsyncSession, cart_id: UUID) -> list[CartItem]:
    stmt = select(CartItem).where(CartItem.cart_id == cart_id).order_by(CartItem.added_at)
    return list((await session.execute(stmt)).scalars().all())


async def count_items(session: AsyncSession, cart_id: UUID) -> int:
    stmt = select(func.count()).select_from(CartItem).where(CartItem.cart_id == cart_id)
    return (await session.execute(stmt)).scalar_one()


async def upsert_item(
    session: AsyncSession, *, cart_id: UUID, sku_id: UUID, product_id: UUID, qty: int
) -> None:
    """**Set** quantity (LLD-06 §2): idempotent, not an increment."""
    now = datetime.now(UTC)
    stmt = (
        pg_insert(CartItem)
        .values(cart_id=cart_id, sku_id=sku_id, product_id=product_id, qty=qty, added_at=now)
        .on_conflict_do_update(
            index_elements=[CartItem.cart_id, CartItem.sku_id],
            set_={"qty": qty},
        )
    )
    await session.execute(stmt)
    await touch_cart(session, cart_id)


async def delete_item(session: AsyncSession, *, cart_id: UUID, sku_id: UUID) -> bool:
    """Idempotent (LLD-06 §2): returns whether a row actually existed, but
    the caller treats both outcomes as success."""
    stmt = (
        delete(CartItem)
        .where(CartItem.cart_id == cart_id, CartItem.sku_id == sku_id)
        .returning(CartItem.sku_id)
    )
    deleted = (await session.execute(stmt)).scalars().one_or_none()
    if deleted is not None:
        await touch_cart(session, cart_id)
    return deleted is not None


async def clear_items(session: AsyncSession, cart_id: UUID) -> int:
    stmt = delete(CartItem).where(CartItem.cart_id == cart_id).returning(CartItem.sku_id)
    deleted = (await session.execute(stmt)).scalars().all()
    if deleted:
        await touch_cart(session, cart_id)
    return len(deleted)


async def mark_expired_carts_abandoned(session: AsyncSession, *, now: datetime) -> int:
    """LLD-06 §2: "a nightly job marks carts abandoned past expires_at"."""
    stmt = (
        update(Cart)
        .where(Cart.status == "active", Cart.expires_at < now)
        .values(status="abandoned", updated_at=now)
        .returning(Cart.id)
    )
    updated = (await session.execute(stmt)).scalars().all()
    return len(updated)
