"""Pure unit tests for the pricing algorithm (LLD-05 §3, §13). No DB, no app
import -- every case here is a pricing decision, not an integration concern."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from app import pricing

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)


def _store(**overrides: object) -> pricing.StoreSettings:
    defaults = dict(
        status="active", currency="usd", shipping_flat_minor=500, tax_rate_bps=875, source_version=1
    )
    return pricing.StoreSettings(**{**defaults, **overrides})  # type: ignore[arg-type]


def _sku(**overrides: object) -> pricing.SkuForQuote:
    defaults = dict(
        sku_id=uuid4(),
        sku_version=1,
        sku_status="active",
        price_minor=1000,
        product_id=uuid4(),
        product_name="Widget",
        product_status="active",
    )
    return pricing.SkuForQuote(**{**defaults, **overrides})  # type: ignore[arg-type]


def test_store_not_active_raises_store_unavailable() -> None:
    sku = _sku()
    with pytest.raises(pricing.StoreUnavailable):
        pricing.compute_quote(
            store=_store(status="suspended"),
            skus_by_id={sku.sku_id: sku},
            lines=[pricing.QuoteLineRequest(sku_id=sku.sku_id, qty=1)],
            promotion=None,
            now=NOW,
        )


def test_simple_quote_computes_subtotal_shipping_and_tax() -> None:
    sku = _sku(price_minor=1000)
    quote = pricing.compute_quote(
        store=_store(shipping_flat_minor=500, tax_rate_bps=875),
        skus_by_id={sku.sku_id: sku},
        lines=[pricing.QuoteLineRequest(sku_id=sku.sku_id, qty=2)],
        promotion=None,
        now=NOW,
    )
    assert quote.valid is True
    assert quote.subtotal_minor == 2000
    assert quote.discount_minor == 0
    assert quote.shipping_minor == 500
    # (2000 - 0 + 500) * 875 / 10000 = 218.75 -> half-up -> 219
    assert quote.tax_minor == 219
    assert quote.total_minor == 2000 + 500 + 219


def test_zero_subtotal_has_no_shipping_or_tax() -> None:
    sku = _sku(sku_status="archived")  # unavailable -> contributes 0
    quote = pricing.compute_quote(
        store=_store(shipping_flat_minor=500, tax_rate_bps=875),
        skus_by_id={sku.sku_id: sku},
        lines=[pricing.QuoteLineRequest(sku_id=sku.sku_id, qty=1)],
        promotion=None,
        now=NOW,
    )
    assert quote.valid is False
    assert quote.subtotal_minor == 0
    assert quote.shipping_minor == 0
    assert quote.tax_minor == 0
    assert quote.total_minor == 0


@pytest.mark.parametrize("sku_status,product_status", [("archived", "active"), ("active", "draft")])
def test_inactive_sku_or_product_marks_the_line_unavailable(
    sku_status: str, product_status: str
) -> None:
    sku = _sku(sku_status=sku_status, product_status=product_status)
    quote = pricing.compute_quote(
        store=_store(),
        skus_by_id={sku.sku_id: sku},
        lines=[pricing.QuoteLineRequest(sku_id=sku.sku_id, qty=1)],
        promotion=None,
        now=NOW,
    )
    assert quote.valid is False
    assert quote.lines[0].available is False
    assert quote.lines[0].line_total_minor == 0


def test_unknown_sku_id_marks_the_line_unavailable_without_crashing() -> None:
    quote = pricing.compute_quote(
        store=_store(),
        skus_by_id={},
        lines=[pricing.QuoteLineRequest(sku_id=uuid4(), qty=1)],
        promotion=None,
        now=NOW,
    )
    assert quote.valid is False
    assert quote.lines[0].available is False


def test_percent_promotion_applies_within_its_window() -> None:
    sku = _sku(price_minor=10000)
    promo = pricing.Promotion(
        code="AUTUMN10",
        kind="percent",
        value=10,
        min_subtotal_minor=0,
        status="active",
        starts_at=datetime(2026, 1, 1, tzinfo=UTC),
        ends_at=datetime(2026, 12, 31, tzinfo=UTC),
    )
    quote = pricing.compute_quote(
        store=_store(),
        skus_by_id={sku.sku_id: sku},
        lines=[pricing.QuoteLineRequest(sku_id=sku.sku_id, qty=1)],
        promotion=promo,
        now=NOW,
    )
    assert quote.discount_minor == 1000  # floor(10000 * 10 / 100)


def test_fixed_promotion_never_discounts_more_than_the_subtotal() -> None:
    sku = _sku(price_minor=500)
    promo = pricing.Promotion(
        code="FIVEOFF",
        kind="fixed",
        value=10_00,
        min_subtotal_minor=0,
        status="active",
        starts_at=datetime(2026, 1, 1, tzinfo=UTC),
        ends_at=datetime(2026, 12, 31, tzinfo=UTC),
    )
    quote = pricing.compute_quote(
        store=_store(shipping_flat_minor=0, tax_rate_bps=0),
        skus_by_id={sku.sku_id: sku},
        lines=[pricing.QuoteLineRequest(sku_id=sku.sku_id, qty=1)],
        promotion=promo,
        now=NOW,
    )
    assert quote.discount_minor == 500  # min(1000, 500), never negative total
    assert quote.total_minor == 0


@pytest.mark.parametrize(
    "now,should_apply",
    [
        (datetime(2025, 12, 31, 23, 59, tzinfo=UTC), False),  # before starts_at
        (datetime(2026, 1, 1, 0, 0, tzinfo=UTC), True),  # exactly starts_at: inclusive
        (datetime(2026, 12, 31, tzinfo=UTC), False),  # exactly ends_at: exclusive
    ],
)
def test_promotion_window_is_half_open(now: datetime, should_apply: bool) -> None:
    sku = _sku(price_minor=10000)
    promo = pricing.Promotion(
        code="WINDOW",
        kind="percent",
        value=10,
        min_subtotal_minor=0,
        status="active",
        starts_at=datetime(2026, 1, 1, tzinfo=UTC),
        ends_at=datetime(2026, 12, 31, tzinfo=UTC),
    )
    quote = pricing.compute_quote(
        store=_store(),
        skus_by_id={sku.sku_id: sku},
        lines=[pricing.QuoteLineRequest(sku_id=sku.sku_id, qty=1)],
        promotion=promo,
        now=now,
    )
    assert (quote.discount_minor > 0) is should_apply


def test_promotion_below_min_subtotal_does_not_apply() -> None:
    sku = _sku(price_minor=100)
    promo = pricing.Promotion(
        code="BIGORDER",
        kind="percent",
        value=10,
        min_subtotal_minor=10000,
        status="active",
        starts_at=datetime(2026, 1, 1, tzinfo=UTC),
        ends_at=datetime(2026, 12, 31, tzinfo=UTC),
    )
    quote = pricing.compute_quote(
        store=_store(),
        skus_by_id={sku.sku_id: sku},
        lines=[pricing.QuoteLineRequest(sku_id=sku.sku_id, qty=1)],
        promotion=promo,
        now=NOW,
    )
    assert quote.discount_minor == 0


def test_disabled_promotion_does_not_apply() -> None:
    sku = _sku(price_minor=10000)
    promo = pricing.Promotion(
        code="OFF",
        kind="percent",
        value=10,
        min_subtotal_minor=0,
        status="disabled",
        starts_at=datetime(2026, 1, 1, tzinfo=UTC),
        ends_at=datetime(2026, 12, 31, tzinfo=UTC),
    )
    quote = pricing.compute_quote(
        store=_store(),
        skus_by_id={sku.sku_id: sku},
        lines=[pricing.QuoteLineRequest(sku_id=sku.sku_id, qty=1)],
        promotion=promo,
        now=NOW,
    )
    assert quote.discount_minor == 0


@pytest.mark.parametrize(
    "amount_minor,bps,expected",
    [
        (2500, 875, 219),  # 218.75 -> half-up -> 219
        (100, 10000, 100),  # 100%
        (0, 875, 0),
        (1, 500, 0),  # 0.05 -> half-up rounds on the .5 of the *unit*, not of 1 -> 0
        (3, 5000, 2),  # 1.5 -> half-up -> 2
    ],
)
def test_tax_rounding_is_half_up(amount_minor: int, bps: int, expected: int) -> None:
    assert pricing._round_half_up_minor(amount_minor, bps) == expected


def test_fingerprint_is_stable_regardless_of_line_order() -> None:
    sku_a, sku_b = _sku(), _sku()
    lines_ab = [
        pricing.QuoteLineRequest(sku_id=sku_a.sku_id, qty=1),
        pricing.QuoteLineRequest(sku_id=sku_b.sku_id, qty=1),
    ]
    lines_ba = list(reversed(lines_ab))
    skus_by_id = {sku_a.sku_id: sku_a, sku_b.sku_id: sku_b}

    quote_ab = pricing.compute_quote(
        store=_store(), skus_by_id=skus_by_id, lines=lines_ab, promotion=None, now=NOW
    )
    quote_ba = pricing.compute_quote(
        store=_store(), skus_by_id=skus_by_id, lines=lines_ba, promotion=None, now=NOW
    )

    assert quote_ab.pricing_fingerprint == quote_ba.pricing_fingerprint


def test_fingerprint_changes_when_settings_version_changes() -> None:
    sku = _sku()
    lines = [pricing.QuoteLineRequest(sku_id=sku.sku_id, qty=1)]
    skus_by_id = {sku.sku_id: sku}

    quote_v1 = pricing.compute_quote(
        store=_store(source_version=1), skus_by_id=skus_by_id, lines=lines, promotion=None, now=NOW
    )
    quote_v2 = pricing.compute_quote(
        store=_store(source_version=2), skus_by_id=skus_by_id, lines=lines, promotion=None, now=NOW
    )

    assert quote_v1.pricing_fingerprint != quote_v2.pricing_fingerprint
