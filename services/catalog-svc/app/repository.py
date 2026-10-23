"""catalog-svc data access (LLD-05 §2, §4)."""

from datetime import UTC, datetime
from uuid import UUID, uuid7

from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Category, PriceHistory, Product, Promotion, Sku, StorePricingSettings
from .pricing import SkuForQuote


class VersionConflict(Exception):
    pass


class NotFoundError(Exception):
    pass


class HasChildren(Exception):
    """A category can't be deleted while it has subcategories or products."""


# --- categories --------------------------------------------------------------


async def insert_category(
    session: AsyncSession,
    *,
    org_id: UUID,
    parent: Category | None,
    name: str,
    slug: str,
) -> Category:
    now = datetime.now(UTC)
    category = Category(
        id=uuid7(),
        org_id=org_id,
        parent_id=parent.id if parent else None,
        path=f"{parent.path}.{slug}" if parent else slug,
        name=name,
        slug=slug,
        created_at=now,
        updated_at=now,
    )
    session.add(category)
    await session.flush()
    return category


async def get_category(session: AsyncSession, category_id: UUID, *, org_id: UUID) -> Category | None:
    stmt = select(Category).where(Category.id == category_id, Category.org_id == org_id)
    return (await session.execute(stmt)).scalars().one_or_none()


async def list_categories(session: AsyncSession, org_id: UUID) -> list[Category]:
    stmt = select(Category).where(Category.org_id == org_id).order_by(Category.path)
    return list((await session.execute(stmt)).scalars().all())


async def update_category(
    session: AsyncSession, category_id: UUID, *, org_id: UUID, name: str
) -> Category:
    stmt = (
        update(Category)
        .where(Category.id == category_id, Category.org_id == org_id)
        .values(name=name, updated_at=datetime.now(UTC))
        .returning(Category)
    )
    row = (await session.execute(stmt)).scalars().one_or_none()
    if row is None:
        raise NotFoundError(category_id)
    return row


async def category_has_children_or_products(
    session: AsyncSession, category_id: UUID, *, org_id: UUID
) -> bool:
    child = await session.execute(
        select(Category.id).where(Category.parent_id == category_id).limit(1)
    )
    if child.scalars().first() is not None:
        return True
    product = await session.execute(
        select(Product.id)
        .where(Product.category_id == category_id, Product.org_id == org_id)
        .limit(1)
    )
    return product.scalars().first() is not None


async def delete_category(session: AsyncSession, category_id: UUID, *, org_id: UUID) -> None:
    if await category_has_children_or_products(session, category_id, org_id=org_id):
        raise HasChildren(category_id)
    result = await session.execute(
        delete(Category).where(Category.id == category_id, Category.org_id == org_id)
    )
    if result.rowcount == 0:
        raise NotFoundError(category_id)


# --- products -----------------------------------------------------------------


async def insert_product(
    session: AsyncSession,
    *,
    org_id: UUID,
    slug: str,
    name: str,
    description_md: str,
    description_html: str,
    category_id: UUID | None,
) -> Product:
    now = datetime.now(UTC)
    product = Product(
        id=uuid7(),
        org_id=org_id,
        slug=slug,
        name=name,
        description_md=description_md,
        description_html=description_html,
        status="draft",
        category_id=category_id,
        version=1,
        created_at=now,
        updated_at=now,
    )
    session.add(product)
    await session.flush()
    return product


async def get_product(session: AsyncSession, product_id: UUID, *, org_id: UUID) -> Product | None:
    stmt = select(Product).where(Product.id == product_id, Product.org_id == org_id)
    return (await session.execute(stmt)).scalars().one_or_none()


async def list_products(
    session: AsyncSession,
    org_id: UUID,
    *,
    status: str | None,
    category_id: UUID | None,
    cursor: tuple[datetime, UUID] | None,
    limit: int,
) -> list[Product]:
    stmt = select(Product).where(Product.org_id == org_id)
    if status is not None:
        stmt = stmt.where(Product.status == status)
    if category_id is not None:
        stmt = stmt.where(Product.category_id == category_id)
    if cursor is not None:
        cursor_updated_at, cursor_id = cursor
        stmt = stmt.where(
            (Product.updated_at < cursor_updated_at)
            | ((Product.updated_at == cursor_updated_at) & (Product.id < cursor_id))
        )
    stmt = stmt.order_by(Product.updated_at.desc(), Product.id.desc()).limit(limit)
    return list((await session.execute(stmt)).scalars().all())


async def update_product(
    session: AsyncSession,
    product_id: UUID,
    *,
    org_id: UUID,
    expected_version: int,
    name: str | None,
    description_md: str | None,
    description_html: str | None,
    category_id: UUID | None,
    category_id_set: bool,
) -> Product:
    values: dict[str, object] = {
        "updated_at": datetime.now(UTC),
        "version": expected_version + 1,
    }
    if name is not None:
        values["name"] = name
    if description_md is not None:
        values["description_md"] = description_md
    if description_html is not None:
        values["description_html"] = description_html
    if category_id_set:
        values["category_id"] = category_id

    stmt = (
        update(Product)
        .where(Product.id == product_id, Product.org_id == org_id, Product.version == expected_version)
        .values(**values)
        .returning(Product)
    )
    row = (await session.execute(stmt)).scalars().one_or_none()
    if row is None:
        raise VersionConflict(product_id)
    return row


async def transition_product_status(
    session: AsyncSession,
    product_id: UUID,
    *,
    org_id: UUID,
    expected_version: int,
    new_status: str,
) -> Product:
    stmt = (
        update(Product)
        .where(Product.id == product_id, Product.org_id == org_id, Product.version == expected_version)
        .values(status=new_status, updated_at=datetime.now(UTC), version=expected_version + 1)
        .returning(Product)
    )
    row = (await session.execute(stmt)).scalars().one_or_none()
    if row is None:
        raise VersionConflict(product_id)
    return row


async def list_skus(session: AsyncSession, product_id: UUID, *, org_id: UUID) -> list[Sku]:
    stmt = select(Sku).where(Sku.product_id == product_id, Sku.org_id == org_id)
    return list((await session.execute(stmt)).scalars().all())


async def bump_product_version(session: AsyncSession, product_id: UUID, *, org_id: UUID) -> Product:
    """A SKU change bumps its parent product's version in the same
    transaction (LLD-05 §4), so the product snapshot event stays coherent."""
    stmt = (
        update(Product)
        .where(Product.id == product_id, Product.org_id == org_id)
        .values(updated_at=datetime.now(UTC), version=Product.version + 1)
        .returning(Product)
    )
    row = (await session.execute(stmt)).scalars().one_or_none()
    if row is None:
        raise NotFoundError(product_id)
    return row


# --- SKUs ----------------------------------------------------------------------


async def get_sku(session: AsyncSession, sku_id: UUID, *, org_id: UUID) -> Sku | None:
    stmt = select(Sku).where(Sku.id == sku_id, Sku.org_id == org_id)
    return (await session.execute(stmt)).scalars().one_or_none()


async def insert_sku(
    session: AsyncSession,
    *,
    sku_id: UUID,
    org_id: UUID,
    product_id: UUID,
    sku_code: str,
    attributes: dict,
    price_minor: int,
    compare_at_minor: int | None,
) -> Sku:
    now = datetime.now(UTC)
    sku = Sku(
        id=sku_id,
        org_id=org_id,
        product_id=product_id,
        sku_code=sku_code,
        attributes=attributes,
        price_minor=price_minor,
        compare_at_minor=compare_at_minor,
        status="active",
        version=1,
        created_at=now,
        updated_at=now,
    )
    session.add(sku)
    await session.flush()
    return sku


async def update_sku(
    session: AsyncSession,
    sku_id: UUID,
    *,
    org_id: UUID,
    expected_version: int,
    sku_code: str,
    attributes: dict,
    price_minor: int,
    compare_at_minor: int | None,
) -> Sku:
    stmt = (
        update(Sku)
        .where(Sku.id == sku_id, Sku.org_id == org_id, Sku.version == expected_version)
        .values(
            sku_code=sku_code,
            attributes=attributes,
            price_minor=price_minor,
            compare_at_minor=compare_at_minor,
            updated_at=datetime.now(UTC),
            version=expected_version + 1,
        )
        .returning(Sku)
    )
    row = (await session.execute(stmt)).scalars().one_or_none()
    if row is None:
        raise VersionConflict(sku_id)
    return row


async def archive_sku(session: AsyncSession, sku_id: UUID, *, org_id: UUID, expected_version: int) -> Sku:
    stmt = (
        update(Sku)
        .where(Sku.id == sku_id, Sku.org_id == org_id, Sku.version == expected_version)
        .values(status="archived", updated_at=datetime.now(UTC), version=expected_version + 1)
        .returning(Sku)
    )
    row = (await session.execute(stmt)).scalars().one_or_none()
    if row is None:
        raise VersionConflict(sku_id)
    return row


async def insert_price_history(
    session: AsyncSession,
    *,
    org_id: UUID,
    sku_id: UUID,
    old_price_minor: int,
    new_price_minor: int,
    actor_id: UUID,
) -> None:
    session.add(
        PriceHistory(
            id=uuid7(),
            org_id=org_id,
            sku_id=sku_id,
            old_price_minor=old_price_minor,
            new_price_minor=new_price_minor,
            actor_id=actor_id,
            occurred_at=datetime.now(UTC),
        )
    )


# --- promotions -----------------------------------------------------------------


async def insert_promotion(
    session: AsyncSession,
    *,
    org_id: UUID,
    code: str,
    kind: str,
    value: int,
    min_subtotal_minor: int,
    starts_at: datetime,
    ends_at: datetime,
) -> Promotion:
    now = datetime.now(UTC)
    promo = Promotion(
        id=uuid7(),
        org_id=org_id,
        code=code.upper(),
        kind=kind,
        value=value,
        min_subtotal_minor=min_subtotal_minor,
        starts_at=starts_at,
        ends_at=ends_at,
        status="active",
        created_at=now,
        updated_at=now,
    )
    session.add(promo)
    await session.flush()
    return promo


async def get_promotion(session: AsyncSession, promotion_id: UUID, *, org_id: UUID) -> Promotion | None:
    stmt = select(Promotion).where(Promotion.id == promotion_id, Promotion.org_id == org_id)
    return (await session.execute(stmt)).scalars().one_or_none()


async def get_promotion_by_code(
    session: AsyncSession, org_id: UUID, code: str
) -> Promotion | None:
    stmt = select(Promotion).where(Promotion.org_id == org_id, Promotion.code == code.upper())
    return (await session.execute(stmt)).scalars().one_or_none()


async def list_promotions(session: AsyncSession, org_id: UUID) -> list[Promotion]:
    stmt = select(Promotion).where(Promotion.org_id == org_id).order_by(Promotion.created_at.desc())
    return list((await session.execute(stmt)).scalars().all())


async def set_promotion_status(
    session: AsyncSession, promotion_id: UUID, *, org_id: UUID, status: str
) -> Promotion:
    stmt = (
        update(Promotion)
        .where(Promotion.id == promotion_id, Promotion.org_id == org_id)
        .values(status=status, updated_at=datetime.now(UTC))
        .returning(Promotion)
    )
    row = (await session.execute(stmt)).scalars().one_or_none()
    if row is None:
        raise NotFoundError(promotion_id)
    return row


async def delete_promotion(session: AsyncSession, promotion_id: UUID, *, org_id: UUID) -> None:
    result = await session.execute(
        delete(Promotion).where(Promotion.id == promotion_id, Promotion.org_id == org_id)
    )
    if result.rowcount == 0:
        raise NotFoundError(promotion_id)


# --- pricing ---------------------------------------------------------------------


async def quote_inputs(
    session: AsyncSession, org_id: UUID, sku_ids: list[UUID]
) -> dict[UUID, SkuForQuote]:
    """§3 step 2: SKUs + their product, in one query, scoped to ``org_id`` so
    a SKU from another org can never be quoted even if its id leaks into a
    request (the cross-tenant case LLD-05 §13 names explicitly)."""
    if not sku_ids:
        return {}
    stmt = (
        select(
            Sku.id,
            Sku.version,
            Sku.status,
            Sku.price_minor,
            Product.id,
            Product.name,
            Product.status,
        )
        .join(Product, Product.id == Sku.product_id)
        .where(Sku.org_id == org_id, Sku.id.in_(sku_ids))
    )
    rows = (await session.execute(stmt)).all()
    return {
        row[0]: SkuForQuote(
            sku_id=row[0],
            sku_version=row[1],
            sku_status=row[2],
            price_minor=row[3],
            product_id=row[4],
            product_name=row[5],
            product_status=row[6],
        )
        for row in rows
    }


async def get_store_settings(session: AsyncSession, org_id: UUID) -> StorePricingSettings | None:
    return await session.get(StorePricingSettings, org_id)


async def upsert_store_settings(
    session: AsyncSession,
    *,
    org_id: UUID,
    status: str,
    currency: str,
    shipping_flat_minor: int,
    tax_rate_bps: int,
    source_version: int,
) -> None:
    """Applied only if ``source_version`` is newer than what's stored (LLD-08
    §7: ``organization.*`` events can arrive out of order across retries)."""
    await session.execute(
        text(
            "INSERT INTO store_pricing_settings "
            "(org_id, status, currency, shipping_flat_minor, tax_rate_bps, source_version, updated_at) "
            "VALUES (:org_id, :status, :currency, :shipping_flat_minor, :tax_rate_bps, "
            " :source_version, now()) "
            "ON CONFLICT (org_id) DO UPDATE SET "
            "status = excluded.status, currency = excluded.currency, "
            "shipping_flat_minor = excluded.shipping_flat_minor, "
            "tax_rate_bps = excluded.tax_rate_bps, source_version = excluded.source_version, "
            "updated_at = excluded.updated_at "
            "WHERE excluded.source_version > store_pricing_settings.source_version"
        ),
        {
            "org_id": org_id,
            "status": status,
            "currency": currency,
            "shipping_flat_minor": shipping_flat_minor,
            "tax_rate_bps": tax_rate_bps,
            "source_version": source_version,
        },
    )
