"""Fake participants standing in for cart-svc, catalog-svc, inventory-svc,
and payments-svc (none of which run during orders-svc's own tests).

``FakePaymentsClient`` keys its scenario off ``payment_method_token``, the
same magic-token scheme LLD-06 §8.1 documents for the real mock provider
(``pm_mock_success``/``pm_mock_decline``/``pm_mock_timeout``) -- so these
tests read the same way a future payments-svc integration test would.
"""

from dataclasses import dataclass
from uuid import UUID, uuid7

import httpx
from app import clients


@dataclass
class _Sku:
    product_id: UUID
    name: str
    price_minor: int
    available: bool


class FakeCartClient:
    def __init__(self) -> None:
        self.carts: dict[tuple[UUID, UUID], clients.CartSnapshot] = {}

    def seed(
        self, *, customer_id: UUID, org_id: UUID, version: int = 1, items: list[tuple[UUID, int]]
    ) -> UUID:
        cart_id = uuid7()
        self.carts[(customer_id, org_id)] = clients.CartSnapshot(
            id=cart_id,
            customer_id=customer_id,
            org_id=org_id,
            version=version,
            items=[
                clients.CartSnapshotLine(sku_id=sku_id, product_id=uuid7(), qty=qty)
                for sku_id, qty in items
            ],
        )
        return cart_id

    async def get_active_snapshot(
        self, *, customer_id: UUID, org_id: UUID
    ) -> clients.CartSnapshot | None:
        return self.carts.get((customer_id, org_id))


class FakeCatalogClient:
    def __init__(self) -> None:
        self.skus: dict[tuple[UUID, UUID], _Sku] = {}
        self.store_unavailable: set[UUID] = set()

    def add(
        self,
        org_id: UUID,
        sku_id: UUID,
        *,
        price_minor: int = 1000,
        available: bool = True,
        name: str = "Test product",
    ) -> None:
        self.skus[(org_id, sku_id)] = _Sku(
            product_id=uuid7(), name=name, price_minor=price_minor, available=available
        )

    async def quote(
        self, *, org_id: UUID, lines: list[tuple[UUID, int]], promo_code: str | None
    ) -> clients.Quote:
        if org_id in self.store_unavailable:
            raise clients.StoreUnavailable(org_id)
        quote_lines = []
        valid = True
        for sku_id, qty in lines:
            sku = self.skus.get((org_id, sku_id))
            if sku is None or not sku.available:
                valid = False
                quote_lines.append(
                    clients.QuoteLine(
                        sku_id=sku_id,
                        product_id=sku.product_id if sku else sku_id,
                        product_name=sku.name if sku else "",
                        qty=qty,
                        available=False,
                        unit_price_minor=0,
                        line_total_minor=0,
                    )
                )
                continue
            quote_lines.append(
                clients.QuoteLine(
                    sku_id=sku_id,
                    product_id=sku.product_id,
                    product_name=sku.name,
                    qty=qty,
                    available=True,
                    unit_price_minor=sku.price_minor,
                    line_total_minor=sku.price_minor * qty,
                )
            )
        subtotal = sum(line.line_total_minor for line in quote_lines if line.available)
        return clients.Quote(
            valid=valid,
            lines=quote_lines,
            subtotal_minor=subtotal,
            discount_minor=0,
            shipping_minor=500 if subtotal > 0 else 0,
            tax_minor=0,
            total_minor=subtotal + (500 if subtotal > 0 else 0),
            currency="EUR",
            pricing_fingerprint="fake-fingerprint",
        )


class FakeInventoryClient:
    def __init__(self) -> None:
        self.stock: dict[tuple[UUID, UUID], int] = {}
        self.reservations: dict[UUID, dict] = {}
        self.committed: set[UUID] = set()
        self.released: dict[UUID, str | None] = {}

    def seed_stock(self, org_id: UUID, sku_id: UUID, qty: int) -> None:
        self.stock[(org_id, sku_id)] = qty

    async def reserve(
        self,
        *,
        order_id: UUID,
        org_id: UUID,
        lines: list[tuple[UUID, int]],
        ttl_seconds: int,
        on_behalf_of: UUID | None,
    ) -> clients.ReserveResult:
        if order_id in self.reservations:
            return clients.ReserveResult(
                reserved=True, reservation_id=self.reservations[order_id]["id"]
            )
        for sku_id, qty in lines:
            if self.stock.get((org_id, sku_id), 0) < qty:
                return clients.ReserveResult(reserved=False, reservation_id=None)
        for sku_id, qty in lines:
            self.stock[(org_id, sku_id)] -= qty
        reservation_id = uuid7()
        self.reservations[order_id] = {"id": reservation_id, "lines": lines, "org_id": org_id}
        return clients.ReserveResult(reserved=True, reservation_id=reservation_id)

    async def commit(self, *, order_id: UUID) -> str:
        if order_id not in self.reservations:
            raise clients.ReservationNotActive(order_id)
        self.committed.add(order_id)
        return "committed"

    async def release(self, *, order_id: UUID, org_id: UUID, reason: str | None) -> str:
        reservation = self.reservations.pop(order_id, None)
        if reservation is not None:
            for sku_id, qty in reservation["lines"]:
                self.stock[(org_id, sku_id)] = self.stock.get((org_id, sku_id), 0) + qty
        self.released[order_id] = reason
        return "released"


class FakePaymentsClient:
    def __init__(self) -> None:
        self.authorized: dict[UUID, clients.AuthorizeResult] = {}

    async def authorize(
        self,
        *,
        order_id: UUID,
        org_id: UUID,
        customer_id: UUID,
        amount_minor: int,
        currency: str,
        payment_method_token: str,
    ) -> clients.AuthorizeResult:
        if order_id in self.authorized:
            return self.authorized[order_id]
        if payment_method_token == "pm_mock_decline":
            raise clients.PaymentDeclined(order_id)
        if payment_method_token == "pm_mock_timeout":
            raise httpx.ConnectError("connection refused (fake)")
        result = clients.AuthorizeResult(payment_id=uuid7(), status="authorized")
        self.authorized[order_id] = result
        return result
