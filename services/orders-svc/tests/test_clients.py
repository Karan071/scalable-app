"""Bulkhead/circuit-breaker/retry wiring in clients.py (LLD-13 §2.2-§2.4).

No real cart-svc/catalog-svc/inventory-svc/payments-svc -- a MockTransport
stands in, same pattern as test_keycloak_admin.py and bff's test_proxy.py.
"""

from collections.abc import Callable
from uuid import uuid4

import httpx
import pytest
from app.clients import (
    CartClient,
    CatalogClient,
    InventoryClient,
    PaymentsClient,
    ServiceTokenSource,
)
from sf_common import Bulkhead, BulkheadFullError, CircuitBreaker, CircuitOpenError, RetryBudget


def _token_source(http_client: httpx.AsyncClient) -> ServiceTokenSource:
    return ServiceTokenSource(
        http_client,
        token_endpoint="https://kc.test/token",
        client_id="sf-svc-orders",
        client_secret="secret",
    )


def _http(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    def with_token(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 300})
        return handler(request)

    return httpx.AsyncClient(transport=httpx.MockTransport(with_token))


async def test_cart_snapshot_goes_through_the_bulkhead_and_breaker() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(404)

    http = _http(handler)
    client = CartClient(
        http,
        base_url="https://cart-svc.test",
        token_source=_token_source(http),
        bulkhead=Bulkhead(5),
        circuit_breaker=CircuitBreaker(
            service="orders-svc", dependency="cart-svc", slow_call_duration_s=4.0
        ),
    )

    result = await client.get_active_snapshot(customer_id=uuid4(), org_id=uuid4())

    assert result is None
    assert calls == 1


async def test_a_full_bulkhead_fails_fast_without_making_the_call() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(404)

    http = _http(handler)
    bulkhead = Bulkhead(0)  # already full: nothing ever gets a slot
    client = CartClient(
        http, base_url="https://cart-svc.test", token_source=_token_source(http), bulkhead=bulkhead
    )

    with pytest.raises(BulkheadFullError):
        await client.get_active_snapshot(customer_id=uuid4(), org_id=uuid4())

    assert calls == 0


async def test_an_open_breaker_fails_fast_without_making_the_call() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    http = _http(handler)
    breaker = CircuitBreaker(
        service="orders-svc", dependency="inventory-svc", slow_call_duration_s=4.0, min_calls=1
    )
    client = InventoryClient(
        http,
        base_url="https://inventory-svc.test",
        token_source=_token_source(http),
        circuit_breaker=breaker,
    )

    # One failing call opens the breaker (min_calls=1, 100% failure rate).
    with pytest.raises(httpx.HTTPStatusError):
        await client.commit(order_id=uuid4())
    assert calls == 1

    with pytest.raises(CircuitOpenError):
        await client.commit(order_id=uuid4())
    assert calls == 1  # the second attempt never reached the handler


async def test_quote_retries_a_503_and_succeeds_on_the_third_attempt() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            return httpx.Response(503)
        return httpx.Response(
            200,
            json={
                "valid": True,
                "lines": [],
                "subtotal_minor": 0,
                "discount_minor": 0,
                "shipping_minor": 0,
                "tax_minor": 0,
                "total_minor": 0,
                "currency": "usd",
                "pricing_fingerprint": "fp1",
            },
        )

    http = _http(handler)
    client = CatalogClient(
        http,
        base_url="https://catalog-svc.test",
        token_source=_token_source(http),
        retry_budget=RetryBudget(retry_ratio=10.0),  # generous: isolate retry behavior itself
    )

    quote = await client.quote(org_id=uuid4(), lines=[], promo_code=None)

    assert quote.valid is True
    assert calls == 3


async def test_reserve_is_not_retried_on_a_503() -> None:
    """Module docstring: state-mutating calls aren't retried here -- that's
    the saga/recovery worker's own job (LLD-06 §5.2)."""
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    http = _http(handler)
    client = InventoryClient(
        http, base_url="https://inventory-svc.test", token_source=_token_source(http)
    )

    with pytest.raises(httpx.HTTPStatusError):
        await client.reserve(
            order_id=uuid4(), org_id=uuid4(), lines=[], ttl_seconds=300, on_behalf_of=None
        )

    assert calls == 1


async def test_a_declined_payment_still_propagates_through_the_guard() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(402)

    http = _http(handler)
    client = PaymentsClient(
        http,
        base_url="https://payments-svc.test",
        token_source=_token_source(http),
        bulkhead=Bulkhead(50),
        circuit_breaker=CircuitBreaker(
            service="orders-svc", dependency="payments-svc", slow_call_duration_s=4.0
        ),
    )

    with pytest.raises(Exception) as exc_info:
        await client.authorize(
            order_id=uuid4(),
            org_id=uuid4(),
            customer_id=uuid4(),
            amount_minor=100,
            currency="usd",
            payment_method_token="pm_mock_decline",
        )
    assert exc_info.typename == "PaymentDeclined"
