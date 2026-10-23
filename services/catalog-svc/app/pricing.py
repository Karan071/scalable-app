"""The pricing quote algorithm (LLD-05 §3, FR-CHK-02).

Pure functions over plain dataclasses — no DB session, no HTTP. The route
layer loads everything in one query (``repository.quote_inputs``) and hands
it here; this module can't be tempted to issue a second query per line, and
every rounding/promotion edge case is unit-testable without Postgres.

catalog-svc is the **pricing authority**: clients never send prices, and
nothing here trusts a client-supplied price.
"""

import hashlib
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID

#: FR-CHK-02 / LLD-05 §3 step 3.
MIN_QTY, MAX_QTY = 1, 99


class StoreUnavailable(Exception):
    """The org isn't ``active`` in the store-settings projection (§3 step 1)."""


@dataclass(frozen=True)
class StoreSettings:
    status: str
    currency: str
    shipping_flat_minor: int
    tax_rate_bps: int
    #: The projection's own version (organization.* event's aggregate
    #: version) — folded into the fingerprint so a shipping/tax change
    #: invalidates any quote computed under the old settings.
    source_version: int


@dataclass(frozen=True)
class QuoteLineRequest:
    sku_id: UUID
    qty: int


@dataclass(frozen=True)
class SkuForQuote:
    """One row of ``repository.quote_inputs``'s single query (§3 step 2)."""

    sku_id: UUID
    sku_version: int
    sku_status: str
    price_minor: int
    product_id: UUID
    product_name: str
    product_status: str


@dataclass(frozen=True)
class Promotion:
    code: str
    kind: str  # "percent" | "fixed"
    value: int
    min_subtotal_minor: int
    status: str
    starts_at: datetime
    ends_at: datetime


@dataclass(frozen=True)
class QuoteLine:
    sku_id: UUID
    product_id: UUID
    product_name: str
    qty: int
    available: bool
    unit_price_minor: int
    sku_version: int
    line_total_minor: int


@dataclass(frozen=True)
class Quote:
    valid: bool
    lines: tuple[QuoteLine, ...]
    subtotal_minor: int
    discount_minor: int
    shipping_minor: int
    tax_minor: int
    total_minor: int
    currency: str
    computed_at: datetime
    pricing_fingerprint: str


def _round_half_up_minor(amount_minor: int, bps: int) -> int:
    """``round(amount_minor * bps / 10000)``, banker's-rounding-free."""
    scaled = Decimal(amount_minor) * Decimal(bps) / Decimal(10000)
    return int(scaled.to_integral_value(rounding=ROUND_HALF_UP))


def _fingerprint(
    *, lines: tuple[QuoteLine, ...], promo_code: str | None, settings_version: int
) -> str:
    # Sorted so line order in the request never changes the fingerprint.
    parts = sorted(f"{line.sku_id}:{line.sku_version}" for line in lines)
    parts.append(f"promo:{promo_code or ''}")
    parts.append(f"settings:{settings_version}")
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def _promotion_applies(promo: Promotion | None, *, subtotal_minor: int, now: datetime) -> bool:
    if promo is None or promo.status != "active":
        return False
    if not (promo.starts_at <= now < promo.ends_at):
        return False
    return subtotal_minor >= promo.min_subtotal_minor


def compute_quote(
    *,
    store: StoreSettings,
    skus_by_id: dict[UUID, SkuForQuote],
    lines: list[QuoteLineRequest],
    promotion: Promotion | None,
    now: datetime,
) -> Quote:
    """§3 steps 1-9. ``lines`` must already have ``MIN_QTY <= qty <= MAX_QTY``
    (the route's request model enforces this — a malformed qty is a 422,
    not a pricing decision)."""
    if store.status != "active":
        raise StoreUnavailable(store.status)

    quote_lines = []
    valid = True
    for req in lines:
        sku = skus_by_id.get(req.sku_id)
        available = sku is not None and sku.sku_status == "active" and sku.product_status == "active"
        if not available:
            valid = False
            quote_lines.append(
                QuoteLine(
                    sku_id=req.sku_id,
                    product_id=sku.product_id if sku else req.sku_id,
                    product_name=sku.product_name if sku else "",
                    qty=req.qty,
                    available=False,
                    unit_price_minor=0,
                    sku_version=sku.sku_version if sku else 0,
                    line_total_minor=0,
                )
            )
            continue
        assert sku is not None  # narrowed by `available`
        quote_lines.append(
            QuoteLine(
                sku_id=sku.sku_id,
                product_id=sku.product_id,
                product_name=sku.product_name,
                qty=req.qty,
                available=True,
                unit_price_minor=sku.price_minor,
                sku_version=sku.sku_version,
                line_total_minor=sku.price_minor * req.qty,
            )
        )

    subtotal_minor = sum(line.line_total_minor for line in quote_lines if line.available)

    discount_minor = 0
    if _promotion_applies(promotion, subtotal_minor=subtotal_minor, now=now):
        assert promotion is not None
        if promotion.kind == "percent":
            discount_minor = subtotal_minor * promotion.value // 100
        else:
            discount_minor = min(promotion.value, subtotal_minor)

    shipping_minor = store.shipping_flat_minor if subtotal_minor > 0 else 0
    taxable_minor = subtotal_minor - discount_minor + shipping_minor
    tax_minor = _round_half_up_minor(taxable_minor, store.tax_rate_bps)
    total_minor = taxable_minor + tax_minor

    lines_tuple = tuple(quote_lines)
    return Quote(
        valid=valid,
        lines=lines_tuple,
        subtotal_minor=subtotal_minor,
        discount_minor=discount_minor,
        shipping_minor=shipping_minor,
        tax_minor=tax_minor,
        total_minor=total_minor,
        currency=store.currency,
        computed_at=now,
        pricing_fingerprint=_fingerprint(
            lines=lines_tuple,
            promo_code=promotion.code if promotion else None,
            settings_version=store.source_version,
        ),
    )
