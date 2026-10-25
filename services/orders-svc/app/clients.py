"""Clients for the checkout saga's synchronous participants (LLD-06 §5.1):
cart-svc, catalog-svc, inventory-svc, and payments-svc.

One ``ServiceTokenSource`` (one ``sf-svc-orders`` client-credentials token)
is shared across all four -- unlike cart-svc/search-svc, which each call
only one other service, orders-svc calls several, and the Keycloak client
scopes (``cart:read``, ``catalog:quotes``, ``inventory:reservations:*``,
``payments:authorize``) all land on the one token at once.
"""

import time
from dataclasses import dataclass
from uuid import UUID

import httpx


class ServiceTokenSource:
    """Caches a client-credentials token for ``sf-svc-orders`` until shortly before expiry."""

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        *,
        token_endpoint: str,
        client_id: str,
        client_secret: str,
    ) -> None:
        self._http = http_client
        self._token_endpoint = token_endpoint
        self._client_id = client_id
        self._client_secret = client_secret
        self._token: str | None = None
        self._expires_at: float = 0.0

    async def get(self) -> str:
        if self._token is not None and time.monotonic() < self._expires_at - 10:
            return self._token
        resp = await self._http.post(
            self._token_endpoint,
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
        )
        resp.raise_for_status()
        body = resp.json()
        self._token = body["access_token"]
        self._expires_at = time.monotonic() + body["expires_in"]
        return self._token


class _BaseClient:
    def __init__(
        self, http_client: httpx.AsyncClient, *, base_url: str, token_source: ServiceTokenSource
    ) -> None:
        self._http = http_client
        self._base_url = base_url.rstrip("/")
        self._tokens = token_source

    async def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {await self._tokens.get()}"}


# --- cart-svc (LLD-06 §2/§3) -------------------------------------------------


@dataclass(frozen=True)
class CartSnapshotLine:
    sku_id: UUID
    product_id: UUID
    qty: int


@dataclass(frozen=True)
class CartSnapshot:
    id: UUID
    customer_id: UUID
    org_id: UUID
    version: int
    items: list[CartSnapshotLine]


class CartClient(_BaseClient):
    async def get_active_snapshot(self, *, customer_id: UUID, org_id: UUID) -> CartSnapshot | None:
        resp = await self._http.get(
            f"{self._base_url}/internal/v1/customers/{customer_id}/carts/{org_id}:snapshot",
            headers=await self._headers(),
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        body = resp.json()
        return CartSnapshot(
            id=UUID(body["id"]),
            customer_id=UUID(body["customer_id"]),
            org_id=UUID(body["org_id"]),
            version=body["version"],
            items=[
                CartSnapshotLine(
                    sku_id=UUID(item["sku_id"]),
                    product_id=UUID(item["product_id"]),
                    qty=item["qty"],
                )
                for item in body["items"]
            ],
        )


# --- catalog-svc (LLD-05 §3) -------------------------------------------------


class StoreUnavailable(Exception):
    pass


@dataclass(frozen=True)
class QuoteLine:
    sku_id: UUID
    product_id: UUID
    product_name: str
    qty: int
    available: bool
    unit_price_minor: int
    line_total_minor: int


@dataclass(frozen=True)
class Quote:
    valid: bool
    lines: list[QuoteLine]
    subtotal_minor: int
    discount_minor: int
    shipping_minor: int
    tax_minor: int
    total_minor: int
    currency: str
    pricing_fingerprint: str


class CatalogClient(_BaseClient):
    async def quote(
        self, *, org_id: UUID, lines: list[tuple[UUID, int]], promo_code: str | None
    ) -> Quote:
        resp = await self._http.post(
            f"{self._base_url}/internal/v1/orgs/{org_id}/quotes",
            headers=await self._headers(),
            json={
                "lines": [{"sku_id": str(sku_id), "qty": qty} for sku_id, qty in lines],
                "promo_code": promo_code,
            },
        )
        if resp.status_code == 409:
            raise StoreUnavailable(org_id)
        resp.raise_for_status()
        body = resp.json()
        return Quote(
            valid=body["valid"],
            lines=[
                QuoteLine(
                    sku_id=UUID(line["sku_id"]),
                    product_id=UUID(line["product_id"]),
                    product_name=line["product_name"],
                    qty=line["qty"],
                    available=line["available"],
                    unit_price_minor=line["unit_price_minor"],
                    line_total_minor=line["line_total_minor"],
                )
                for line in body["lines"]
            ],
            subtotal_minor=body["subtotal_minor"],
            discount_minor=body["discount_minor"],
            shipping_minor=body["shipping_minor"],
            tax_minor=body["tax_minor"],
            total_minor=body["total_minor"],
            currency=body["currency"],
            pricing_fingerprint=body["pricing_fingerprint"],
        )


# --- inventory-svc (LLD-07 §4) ----------------------------------------------


@dataclass(frozen=True)
class ReserveResult:
    reserved: bool
    reservation_id: UUID | None


class ReservationNotActive(Exception):
    pass


class ReservationCommitted(Exception):
    pass


class InventoryClient(_BaseClient):
    async def reserve(
        self,
        *,
        order_id: UUID,
        org_id: UUID,
        lines: list[tuple[UUID, int]],
        ttl_seconds: int,
        on_behalf_of: UUID | None,
    ) -> ReserveResult:
        resp = await self._http.post(
            f"{self._base_url}/internal/v1/reservations",
            headers=await self._headers(),
            json={
                "order_id": str(order_id),
                "org_id": str(org_id),
                "lines": [{"sku_id": str(sku_id), "qty": qty} for sku_id, qty in lines],
                "ttl_seconds": ttl_seconds,
                "on_behalf_of": str(on_behalf_of) if on_behalf_of else None,
            },
        )
        if resp.status_code == 409:
            return ReserveResult(reserved=False, reservation_id=None)
        resp.raise_for_status()
        body = resp.json()
        return ReserveResult(reserved=True, reservation_id=UUID(body["reservation_id"]))

    async def commit(self, *, order_id: UUID) -> str:
        resp = await self._http.post(
            f"{self._base_url}/internal/v1/reservations/{order_id}:commit",
            headers=await self._headers(),
        )
        if resp.status_code == 409:
            raise ReservationNotActive(order_id)
        resp.raise_for_status()
        return str(resp.json()["status"])

    async def release(self, *, order_id: UUID, org_id: UUID, reason: str | None) -> str:
        resp = await self._http.post(
            f"{self._base_url}/internal/v1/reservations/{order_id}:release",
            headers=await self._headers(),
            json={"org_id": str(org_id), "reason": reason},
        )
        if resp.status_code == 409:
            raise ReservationCommitted(order_id)
        resp.raise_for_status()
        return str(resp.json()["status"])


# --- payments-svc (LLD-06 §7, doesn't exist yet -- E3.6) --------------------


@dataclass(frozen=True)
class AuthorizeResult:
    payment_id: UUID
    status: str


class PaymentDeclined(Exception):
    pass


class PaymentPending(Exception):
    """payments-svc's own provider call timed out (LLD-06 §7.2's
    "unknown" state) -- it answered fast with 202 rather than hanging
    until its own budget expired, so orders-svc can move to
    ``AUTH_PENDING`` immediately instead of also timing out itself."""


class PaymentsClient(_BaseClient):
    async def authorize(
        self,
        *,
        order_id: UUID,
        org_id: UUID,
        customer_id: UUID,
        amount_minor: int,
        currency: str,
        payment_method_token: str,
    ) -> AuthorizeResult:
        resp = await self._http.post(
            f"{self._base_url}/internal/v1/payments",
            headers=await self._headers(),
            json={
                "order_id": str(order_id),
                "org_id": str(org_id),
                "customer_id": str(customer_id),
                "amount_minor": amount_minor,
                "currency": currency,
                "payment_method_token": payment_method_token,
            },
        )
        if resp.status_code == 402:
            raise PaymentDeclined(order_id)
        if resp.status_code == 202:
            raise PaymentPending(order_id)
        resp.raise_for_status()
        body = resp.json()
        return AuthorizeResult(payment_id=UUID(body["payment_id"]), status=body["status"])
