"""catalog-svc business logic (LLD-05 §3-§4).

Deferred to a later slice (LLD-05 §14, this service's own open items):
product images, promotion redemption limits, and the full-text search
index (a separate service). The storefront's public read endpoints
(LLD-05 §6) also wait for inventory-svc, which doesn't exist yet — the
composition they need is BFF-side, not catalog-svc's own.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

import nh3
from markdown_it import MarkdownIt
from sf_common import get_correlation_id
from sf_messaging import add_to_outbox, build_envelope, record_audit
from sf_messaging.audit import ActorType
from sqlalchemy.ext.asyncio import AsyncSession

from . import pricing, repository
from .models import Category, Product, Promotion, Sku
from .slugs import normalize_slug

_NOTIFY_CHANNEL = "outbox_catalog"
_MD = MarkdownIt("commonmark")

# LLD-05 §2: Markdown -> HTML -> allow-list sanitize. No raw HTML, scripts,
# or event handlers; links are forced rel="nofollow noopener". The frontend
# sanitizes again on render (LLD-15 §6) -- this is defense in depth, not the
# only check.
_ALLOWED_TAGS = {
    "p", "br", "b", "strong", "i", "em", "ul", "ol", "li", "a",
    "code", "pre", "blockquote", "h1", "h2", "h3", "h4",
}
_ALLOWED_ATTRIBUTES = {"a": {"href"}}


class NotFoundError(Exception):
    pass


class VersionConflictError(Exception):
    pass


class HasChildrenError(Exception):
    pass


class MissingIfMatchError(Exception):
    """A PUT against an existing SKU with no ``If-Match`` (§4: 428)."""


class InvalidTransition(Exception):
    pass


_PRODUCT_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "publish": ("draft", "archived"),
    "archive": ("draft", "active"),
}


def render_description(markdown_text: str) -> str:
    raw_html = _MD.render(markdown_text)
    return nh3.clean(
        raw_html, tags=_ALLOWED_TAGS, attributes=_ALLOWED_ATTRIBUTES, link_rel="nofollow noopener"
    )


async def _emit(
    session: AsyncSession,
    *,
    event_type: str,
    routing_key: str,
    aggregate_type: str,
    aggregate_id: UUID,
    aggregate_version: int,
    org_id: UUID,
    data: dict[str, object],
) -> None:
    envelope = build_envelope(
        event_type=event_type,
        source="catalog-svc",
        subject=str(aggregate_id),
        data=data,
        schema_version="1.0",
        aggregate_type=aggregate_type,
        aggregate_version=aggregate_version,
        org_id=str(org_id),
    )
    await add_to_outbox(
        session,
        envelope,
        exchange="sf.events",
        routing_key=routing_key,
        aggregate_id=str(aggregate_id),
        notify_channel=_NOTIFY_CHANNEL,
    )


async def _audit(
    session: AsyncSession,
    *,
    action: str,
    actor_id: UUID | str,
    org_id: UUID,
    target_type: str,
    target_id: UUID,
    aggregate_version: int,
    actor_type: ActorType = "user",
    changes: dict | None = None,
) -> None:
    await record_audit(
        session,
        notify_channel=_NOTIFY_CHANNEL,
        source_service="catalog-svc",
        action=action,
        actor_type=actor_type,
        actor_id=str(actor_id),
        domain=f"org:{org_id}",
        target_type=target_type,
        target_id=str(target_id),
        aggregate_version=aggregate_version,
        changes=changes,
        correlation_id=get_correlation_id(),
    )


def _product_snapshot_data(product: Product, skus: list[Sku]) -> dict[str, object]:
    return {
        "org_id": str(product.org_id),
        "product_id": str(product.id),
        "slug": product.slug,
        "name": product.name,
        "status": product.status,
        "category_id": str(product.category_id) if product.category_id else None,
        "version": product.version,
        "skus": [
            {
                "id": str(sku.id),
                "sku_code": sku.sku_code,
                "price_minor": sku.price_minor,
                "status": sku.status,
                "attributes": sku.attributes,
            }
            for sku in skus
        ],
    }


async def _emit_product_snapshot(session: AsyncSession, product: Product) -> None:
    skus = await repository.list_skus(session, product.id, org_id=product.org_id)
    await _emit(
        session,
        event_type="product.updated",
        routing_key="product.updated.v1",
        aggregate_type="product",
        aggregate_id=product.id,
        aggregate_version=product.version,
        org_id=product.org_id,
        data=_product_snapshot_data(product, skus),
    )


# --- categories ------------------------------------------------------------------


async def create_category(
    session: AsyncSession, *, org_id: UUID, name: str, slug: str, parent_id: UUID | None
) -> Category:
    normalized = normalize_slug(slug)
    parent = None
    if parent_id is not None:
        parent = await repository.get_category(session, parent_id, org_id=org_id)
        if parent is None:
            raise NotFoundError(parent_id)
    return await repository.insert_category(
        session, org_id=org_id, parent=parent, name=name, slug=normalized
    )


async def update_category(session: AsyncSession, category_id: UUID, *, org_id: UUID, name: str) -> Category:
    try:
        return await repository.update_category(session, category_id, org_id=org_id, name=name)
    except repository.NotFoundError as exc:
        raise NotFoundError(category_id) from exc


async def delete_category(session: AsyncSession, category_id: UUID, *, org_id: UUID) -> None:
    try:
        await repository.delete_category(session, category_id, org_id=org_id)
    except repository.NotFoundError as exc:
        raise NotFoundError(category_id) from exc
    except repository.HasChildren as exc:
        raise HasChildrenError(category_id) from exc


# --- products --------------------------------------------------------------------


async def create_product(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    slug: str,
    name: str,
    description_md: str,
    category_id: UUID | None,
) -> Product:
    normalized = normalize_slug(slug)
    if category_id is not None and await repository.get_category(session, category_id, org_id=org_id) is None:
        raise NotFoundError(category_id)
    product = await repository.insert_product(
        session,
        org_id=org_id,
        slug=normalized,
        name=name,
        description_md=description_md,
        description_html=render_description(description_md),
        category_id=category_id,
    )
    await _audit(
        session,
        action="product.created",
        actor_id=actor_id,
        org_id=org_id,
        target_type="product",
        target_id=product.id,
        aggregate_version=product.version,
        changes={"name": {"to": product.name}, "slug": {"to": product.slug}},
    )
    await _emit_product_snapshot(session, product)
    return product


async def update_product(
    session: AsyncSession,
    product_id: UUID,
    *,
    org_id: UUID,
    actor_id: UUID,
    expected_version: int,
    name: str | None,
    description_md: str | None,
    category_id: UUID | None,
    category_id_set: bool,
) -> Product:
    if category_id_set and category_id is not None:
        if await repository.get_category(session, category_id, org_id=org_id) is None:
            raise NotFoundError(category_id)
    try:
        product = await repository.update_product(
            session,
            product_id,
            org_id=org_id,
            expected_version=expected_version,
            name=name,
            description_md=description_md,
            description_html=render_description(description_md) if description_md is not None else None,
            category_id=category_id,
            category_id_set=category_id_set,
        )
    except repository.VersionConflict as exc:
        raise VersionConflictError(product_id) from exc
    await _audit(
        session,
        action="product.updated",
        actor_id=actor_id,
        org_id=org_id,
        target_type="product",
        target_id=product.id,
        aggregate_version=product.version,
        changes={k: {"to": v} for k, v in {"name": name}.items() if v is not None},
    )
    await _emit_product_snapshot(session, product)
    return product


async def transition_product(
    session: AsyncSession,
    product_id: UUID,
    *,
    org_id: UUID,
    actor_id: UUID,
    expected_version: int,
    action: str,
) -> Product:
    current = await repository.get_product(session, product_id, org_id=org_id)
    if current is None:
        raise NotFoundError(product_id)
    if current.status not in _PRODUCT_TRANSITIONS[action]:
        raise InvalidTransition(f"cannot {action} a product in status {current.status!r}")
    new_status = "active" if action == "publish" else "archived"
    try:
        product = await repository.transition_product_status(
            session, product_id, org_id=org_id, expected_version=expected_version, new_status=new_status
        )
    except repository.VersionConflict as exc:
        raise VersionConflictError(product_id) from exc
    await _audit(
        session,
        action=f"product.{action}d" if action == "publish" else "product.archived",
        actor_id=actor_id,
        org_id=org_id,
        target_type="product",
        target_id=product.id,
        aggregate_version=product.version,
        changes={"status": {"to": product.status}},
    )
    await _emit_product_snapshot(session, product)
    return product


# --- SKUs ---------------------------------------------------------------------


async def put_sku(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    product_id: UUID,
    sku_id: UUID,
    if_match: int | None,
    sku_code: str,
    attributes: dict,
    price_minor: int,
    compare_at_minor: int | None,
) -> Sku:
    product = await repository.get_product(session, product_id, org_id=org_id)
    if product is None:
        raise NotFoundError(product_id)

    existing = await repository.get_sku(session, sku_id, org_id=org_id)
    if existing is None:
        sku = await repository.insert_sku(
            session,
            sku_id=sku_id,
            org_id=org_id,
            product_id=product_id,
            sku_code=sku_code,
            attributes=attributes,
            price_minor=price_minor,
            compare_at_minor=compare_at_minor,
        )
        old_price_minor = None
    else:
        if if_match is None:
            raise MissingIfMatchError(sku_id)
        try:
            sku = await repository.update_sku(
                session,
                sku_id,
                org_id=org_id,
                expected_version=if_match,
                sku_code=sku_code,
                attributes=attributes,
                price_minor=price_minor,
                compare_at_minor=compare_at_minor,
            )
        except repository.VersionConflict as exc:
            raise VersionConflictError(sku_id) from exc
        old_price_minor = existing.price_minor

    product = await repository.bump_product_version(session, product_id, org_id=org_id)

    if old_price_minor is not None and old_price_minor != sku.price_minor:
        await repository.insert_price_history(
            session,
            org_id=org_id,
            sku_id=sku.id,
            old_price_minor=old_price_minor,
            new_price_minor=sku.price_minor,
            actor_id=actor_id,
        )
        await _emit(
            session,
            event_type="price.changed",
            routing_key="price.changed.v1",
            aggregate_type="sku",
            aggregate_id=sku.id,
            aggregate_version=sku.version,
            org_id=org_id,
            data={
                "sku_id": str(sku.id),
                "old_price_minor": old_price_minor,
                "new_price_minor": sku.price_minor,
                "actor_id": str(actor_id),
            },
        )

    await _audit(
        session,
        action="sku.updated" if old_price_minor is not None else "sku.created",
        actor_id=actor_id,
        org_id=org_id,
        target_type="sku",
        target_id=sku.id,
        aggregate_version=sku.version,
        changes={"price_minor": {"to": sku.price_minor}},
    )
    await _emit_product_snapshot(session, product)
    return sku


async def archive_sku(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    product_id: UUID,
    sku_id: UUID,
    if_match: int,
) -> None:
    try:
        await repository.archive_sku(session, sku_id, org_id=org_id, expected_version=if_match)
    except repository.VersionConflict as exc:
        raise VersionConflictError(sku_id) from exc
    product = await repository.bump_product_version(session, product_id, org_id=org_id)
    await _audit(
        session,
        action="sku.archived",
        actor_id=actor_id,
        org_id=org_id,
        target_type="sku",
        target_id=sku_id,
        aggregate_version=product.version,
    )
    await _emit_product_snapshot(session, product)


# --- promotions ------------------------------------------------------------------


class InvalidPromotionWindow(Exception):
    pass


async def create_promotion(
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
    if starts_at >= ends_at:
        raise InvalidPromotionWindow("starts_at must be before ends_at")
    return await repository.insert_promotion(
        session,
        org_id=org_id,
        code=code,
        kind=kind,
        value=value,
        min_subtotal_minor=min_subtotal_minor,
        starts_at=starts_at,
        ends_at=ends_at,
    )


async def set_promotion_status(
    session: AsyncSession, promotion_id: UUID, *, org_id: UUID, status: str
) -> Promotion:
    try:
        return await repository.set_promotion_status(session, promotion_id, org_id=org_id, status=status)
    except repository.NotFoundError as exc:
        raise NotFoundError(promotion_id) from exc


async def delete_promotion(session: AsyncSession, promotion_id: UUID, *, org_id: UUID) -> None:
    try:
        await repository.delete_promotion(session, promotion_id, org_id=org_id)
    except repository.NotFoundError as exc:
        raise NotFoundError(promotion_id) from exc


# --- pricing ----------------------------------------------------------------------


@dataclass(frozen=True)
class QuoteRequest:
    lines: list[pricing.QuoteLineRequest]
    promo_code: str | None


async def quote(session: AsyncSession, org_id: UUID, request: QuoteRequest) -> pricing.Quote:
    """LLD-05 §3. Raises ``pricing.StoreUnavailable`` (-> 409) if the store
    isn't ``active`` in the ``store_pricing_settings`` projection."""
    settings_row = await repository.get_store_settings(session, org_id)
    store = pricing.StoreSettings(
        status=settings_row.status if settings_row else "unknown",
        currency=settings_row.currency if settings_row else "usd",
        shipping_flat_minor=settings_row.shipping_flat_minor if settings_row else 0,
        tax_rate_bps=settings_row.tax_rate_bps if settings_row else 0,
        source_version=settings_row.source_version if settings_row else 0,
    )
    sku_ids = [line.sku_id for line in request.lines]
    skus_by_id = await repository.quote_inputs(session, org_id, sku_ids)

    promotion = None
    if request.promo_code:
        row = await repository.get_promotion_by_code(session, org_id, request.promo_code)
        if row is not None:
            promotion = pricing.Promotion(
                code=row.code,
                kind=row.kind,
                value=row.value,
                min_subtotal_minor=row.min_subtotal_minor,
                status=row.status,
                starts_at=row.starts_at,
                ends_at=row.ends_at,
            )

    return pricing.compute_quote(
        store=store,
        skus_by_id=skus_by_id,
        lines=request.lines,
        promotion=promotion,
        now=datetime.now(UTC),
    )
