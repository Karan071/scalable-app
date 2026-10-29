"""Clients for the checkout saga's synchronous participants (LLD-06 §5.1):
cart-svc, catalog-svc, inventory-svc, and payments-svc.

One ``ServiceTokenSource`` (one ``sf-svc-orders`` client-credentials token)
is shared across all four -- unlike cart-svc/search-svc, which each call
only one other service, orders-svc calls several, and the Keycloak client
scopes (``cart:read``, ``catalog:quotes``, ``inventory:reservations:*``,
``payments:authorize``) all land on the one token at once.

Each client optionally takes a ``Bulkhead``/``CircuitBreaker`` pair (LLD-13
§2.3-§2.4: "orders → payments: 50; orders → inventory: 100"); both default
to ``None`` so a fake client built for a unit test (``test_checkout.py``'s
own ``FakeCartClient`` etc.) is unaffected -- this only wraps the real
client's own HTTP call, not the ``Participants`` interface. Retries
(LLD-13 §2.2) are deliberately *not* added here for the state-mutating
calls (reserve/commit/release/authorize): each already has its own
idempotency story at the saga layer (the recovery worker, LLD-06 §5.2),
and blindly retrying them at the transport layer risks interacting with
that in ways this pass didn't verify -- only the read-only, side-effect-
free calls (cart snapshot, catalog quote) retry.
"""

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar
from uuid import UUID

import httpx
from sf_common import Bulkhead, CircuitBreaker, RetryBudget, send_with_retries

T = TypeVar("T")


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


class _DependencyFailure(Exception):
    """Internal only (``_BaseClient._one_attempt``'s own docstring) --
    never escapes a client method."""

    def __init__(self, response: httpx.Response) -> None:
        self.response = response


class _BaseClient:
    def __init__(
        self,
        http_client: httpx.AsyncClient,
        *,
        base_url: str,
        token_source: ServiceTokenSource,
        bulkhead: Bulkhead | None = None,
        circuit_breaker: CircuitBreaker | None = None,
        retry_budget: RetryBudget | None = None,
    ) -> None:
        self._http = http_client
        self._base_url = base_url.rstrip("/")
        self._tokens = token_source
        self._bulkhead = bulkhead
        self._breaker = circuit_breaker
        self._retry_budget = retry_budget

    async def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {await self._tokens.get()}"}

    async def _one_attempt(self, send: Callable[[], Awaitable[T]]) -> T:
        """Circuit breaker around exactly one outbound call (LLD-13 §2.3).
        Not wrapped in the bulkhead itself -- callers that retry (LLD-13
        §2.2) hold one bulkhead slot across every attempt, not one per
        attempt, since a retry sequence is still one logical call in
        flight from the dependency's point of view.

        ``CircuitBreaker.call`` only sees exceptions, but httpx doesn't
        raise on a 5xx response by itself -- every call site here still
        inspects a response's own status code afterwards (409, 402, 202,
        ...), so a 5xx is turned into an exception just long enough for
        the breaker to record it as a failure, then turned straight back
        into the same response. 4xx is never a breaker failure: that's a
        client-side/business outcome, not a sign the dependency is down.
        """
        if self._breaker is None:
            return await send()

        async def tracked() -> T:
            result = await send()
            if isinstance(result, httpx.Response) and result.status_code >= 500:
                raise _DependencyFailure(result)
            return result

        try:
            return await self._breaker.call(tracked)
        except _DependencyFailure as exc:
            return exc.response  # type: ignore[return-value]

    async def _guarded(self, send: Callable[[], Awaitable[T]]) -> T:
        """A single, non-retried call behind the bulkhead + breaker (LLD-13
        §2.3-§2.4) -- for every state-mutating call (module docstring).
        Either guard defaulting to ``None`` (every fake client, and any
        real client this epic didn't get to) makes this a plain call,
        unchanged from before."""
        attempt = lambda: self._one_attempt(send)  # noqa: E731
        if self._bulkhead is None:
            return await attempt()
        async with self._bulkhead.guard():
            return await attempt()

    async def _guarded_idempotent(
        self, send: Callable[[], Awaitable[httpx.Response]]
    ) -> httpx.Response:
        """``_guarded`` plus retries (LLD-13 §2.2) -- only for calls with no
        side effects (GET reads, or a pure computation like a pricing
        quote; module docstring)."""
        if self._retry_budget is None:
            return await self._guarded(send)

        async def attempt_with_retries() -> httpx.Response:
            return await send_with_retries(
                lambda: self._one_attempt(send),
                idempotent=True,
                budget=self._retry_budget,  # type: ignore[arg-type]
                retryable_errors=(httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout),
            )

        if self._bulkhead is None:
            return await attempt_with_retries()
        async with self._bulkhead.guard():
            return await attempt_with_retries()


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
        url = f"{self._base_url}/internal/v1/customers/{customer_id}/carts/{org_id}:snapshot"

        async def send() -> httpx.Response:
            return await self._http.get(url, headers=await self._headers())

        resp = await self._guarded_idempotent(send)
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
        url = f"{self._base_url}/internal/v1/orgs/{org_id}/quotes"
        body = {
            "lines": [{"sku_id": str(sku_id), "qty": qty} for sku_id, qty in lines],
            "promo_code": promo_code,
        }

        async def send() -> httpx.Response:
            return await self._http.post(url, headers=await self._headers(), json=body)

        # A quote is a pure computation (no mutation), so it's safe to
        # retry even though the verb is POST (module docstring).
        resp = await self._guarded_idempotent(send)
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
        url = f"{self._base_url}/internal/v1/reservations"
        body = {
            "order_id": str(order_id),
            "org_id": str(org_id),
            "lines": [{"sku_id": str(sku_id), "qty": qty} for sku_id, qty in lines],
            "ttl_seconds": ttl_seconds,
            "on_behalf_of": str(on_behalf_of) if on_behalf_of else None,
        }

        async def send() -> httpx.Response:
            return await self._http.post(url, headers=await self._headers(), json=body)

        resp = await self._guarded(send)
        if resp.status_code == 409:
            return ReserveResult(reserved=False, reservation_id=None)
        resp.raise_for_status()
        reserve_body = resp.json()
        return ReserveResult(reserved=True, reservation_id=UUID(reserve_body["reservation_id"]))

    async def commit(self, *, order_id: UUID) -> str:
        url = f"{self._base_url}/internal/v1/reservations/{order_id}:commit"

        async def send() -> httpx.Response:
            return await self._http.post(url, headers=await self._headers())

        resp = await self._guarded(send)
        if resp.status_code == 409:
            raise ReservationNotActive(order_id)
        resp.raise_for_status()
        return str(resp.json()["status"])

    async def release(self, *, order_id: UUID, org_id: UUID, reason: str | None) -> str:
        url = f"{self._base_url}/internal/v1/reservations/{order_id}:release"
        body = {"org_id": str(org_id), "reason": reason}

        async def send() -> httpx.Response:
            return await self._http.post(url, headers=await self._headers(), json=body)

        resp = await self._guarded(send)
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
        url = f"{self._base_url}/internal/v1/payments"
        body = {
            "order_id": str(order_id),
            "org_id": str(org_id),
            "customer_id": str(customer_id),
            "amount_minor": amount_minor,
            "currency": currency,
            "payment_method_token": payment_method_token,
        }

        async def send() -> httpx.Response:
            return await self._http.post(url, headers=await self._headers(), json=body)

        resp = await self._guarded(send)
        if resp.status_code == 402:
            raise PaymentDeclined(order_id)
        if resp.status_code == 202:
            raise PaymentPending(order_id)
        resp.raise_for_status()
        auth_body = resp.json()
        return AuthorizeResult(payment_id=UUID(auth_body["payment_id"]), status=auth_body["status"])
