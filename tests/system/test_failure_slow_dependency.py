"""E4.8 / LLD-13 §3 FS-3: slow dependency via the Compose `toxiproxy`
container.

    make test-system

Real catalog-svc, cart-svc, inventory-svc, orders-svc, with the fake IdP.
orders-svc's own `SF_INVENTORY_SVC_BASE_URL` points at a toxiproxy proxy
(not inventory-svc directly); the proxy's upstream is inventory-svc's real
port (`tests/system/toxiproxy.py`'s own module docstring explains the
`host.docker.internal` plumbing). Everything else is unproxied -- only the
orders -> inventory hop is slowed, same as the LLD's own "payments ->
provider; then inventory" scenario's second half.

This is the one real caller circuit breakers/bulkheads were wired into in
E4.6 (`services/orders-svc/app/clients.py`), so it's also what proves LLD-13
§2.3's "fail fast with a typed error -> 503 dependency-unavailable" promise
end to end -- which E4.8's own failure injection found was NOT kept before
this epic (`CircuitOpenError`/`BulkheadFullError` fell through to a generic
500; see `sf_common.install_problem_details`'s `dependency_unavailable_errors`
param and its new orders-svc wiring).

Not covered here: payments -> provider latency (payments-svc's own HTTP
client has no breaker/bulkhead at all yet -- E4.6's own "as built" note in
docs/00-delivery-plan.md already names this as wired into exactly one real
caller, orders-svc). FS-3's PostgreSQL/Redis slow-dependency variants: not
attempted (no tool in this stack injects DB/Redis-level latency).
"""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID, uuid4, uuid7

import asyncpg
import httpx
import pytest
import pytest_asyncio
from harness import ENV, TEST_DB, FakeIdP, ServiceProcess, service_env
from toxiproxy import ToxiproxyClient

pytestmark = pytest.mark.asyncio(loop_scope="module")

_PORTS = {
    "catalog-svc": 18161,
    "cart-svc": 18162,
    "inventory-svc": 18163,
    "orders-svc": 18164,
}
_TOXIPROXY_LISTEN_PORT = 20000
_STOCK = 10_000
# orders-svc's own http_client timeout (services/orders-svc/app/main.py) is
# 5s total -- latency past that reliably trips a ReadTimeout. 7s leaves
# clear margin without the test itself waiting any longer than it has to.
_LATENCY_MS = 7_000
# >= CircuitBreaker's own min_calls (10, LLD-13 §2.3): firing this many
# concurrent, all-failing calls and awaiting every one of them before
# asserting anything makes the breaker's open transition deterministic --
# no polling or timing guesswork (CircuitBreaker._record runs synchronously
# right after each failed call, before gather() can return).
_N_TRIP_CALLS = 15

_ORDERS_SCOPE = (
    "cart:read catalog:quotes inventory:reservations:write "
    "inventory:reservations:read payments:authorize"
)
_ADDRESS = {
    "name": "A. Customer",
    "line1": "1 Main St",
    "city": "Berlin",
    "postal_code": "10115",
    "country": "DE",
}


async def _db(role: str) -> asyncpg.Connection:
    return await asyncpg.connect(
        host="localhost", database=TEST_DB, user=role, password=ENV["POSTGRES_APP_PASSWORD"]
    )


async def _grant_customer_roles(user_ids: list[UUID]) -> None:
    conn = await _db("svc_authz")
    try:
        await conn.executemany(
            "INSERT INTO casbin_rule (ptype, v0, v1, v2) "
            "VALUES ('g', $1, 'role:customer', 'customer')",
            [(f"user:{uid}",) for uid in user_ids],
        )
    finally:
        await conn.close()


async def _seed_catalog(*, org_id: UUID, product_id: UUID, sku_id: UUID, price_minor: int) -> None:
    conn = await _db("svc_catalog")
    try:
        await conn.execute(
            "INSERT INTO store_pricing_settings "
            "(org_id, status, currency, shipping_flat_minor, tax_rate_bps, source_version, "
            " updated_at) "
            "VALUES ($1, 'active', 'usd', 0, 0, 1, now())",
            org_id,
        )
        await conn.execute(
            "INSERT INTO products "
            "(id, org_id, slug, name, description_md, description_html, status, version, "
            " created_at, updated_at) "
            "VALUES ($1, $2, $3, 'Failure-lab product', '', '', 'active', 1, now(), now())",
            product_id,
            org_id,
            f"fl-{product_id}",
        )
        await conn.execute(
            "INSERT INTO skus "
            "(id, org_id, product_id, sku_code, attributes, price_minor, status, version, "
            " created_at, updated_at) "
            "VALUES ($1, $2, $3, $4, '{}'::jsonb, $5, 'active', 1, now(), now())",
            sku_id,
            org_id,
            product_id,
            f"FL-{sku_id}",
            price_minor,
        )
    finally:
        await conn.close()


async def _seed_stock(*, org_id: UUID, product_id: UUID, sku_id: UUID, on_hand: int) -> None:
    conn = await _db("svc_inventory")
    try:
        await conn.execute(
            "INSERT INTO stock_items "
            "(sku_id, org_id, product_id, on_hand, reserved, low_stock_threshold, version, "
            " updated_at) "
            "VALUES ($1, $2, $3, $4, 0, 0, 1, now())",
            sku_id,
            org_id,
            product_id,
            on_hand,
        )
    finally:
        await conn.close()


async def _seed_carts(carts: list[tuple[UUID, UUID, UUID, UUID, UUID, int]]) -> None:
    conn = await _db("svc_cart")
    try:
        await conn.executemany(
            "INSERT INTO carts "
            "(id, customer_id, org_id, status, version, created_at, updated_at, expires_at) "
            "VALUES ($1, $2, $3, 'active', 1, now(), now(), now() + interval '1 day')",
            [(cart_id, customer_id, org_id) for cart_id, customer_id, org_id, *_ in carts],
        )
        await conn.executemany(
            "INSERT INTO cart_items (cart_id, sku_id, product_id, qty, added_at) "
            "VALUES ($1, $2, $3, $4, now())",
            [
                (cart_id, sku_id, product_id, qty)
                for cart_id, _customer_id, _org_id, sku_id, product_id, qty in carts
            ],
        )
    finally:
        await conn.close()


@dataclass
class Fixture:
    base: dict[str, str]
    idp: FakeIdP
    toxiproxy: ToxiproxyClient
    org_id: UUID
    product_id: UUID
    sku_id: UUID
    customer_ids: list[UUID]


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def fixture() -> AsyncIterator[Fixture]:
    toxiproxy = ToxiproxyClient()
    if not await toxiproxy.reachable():
        await toxiproxy.aclose()
        pytest.skip("toxiproxy admin API not reachable on localhost:8474")

    try:
        conn = await _db("svc_authz")
        await conn.close()
    except Exception as exc:
        await toxiproxy.aclose()
        pytest.skip(f"local Postgres not reachable: {exc}")

    org_id, product_id, sku_id = uuid4(), uuid4(), uuid4()
    # [0]=baseline, [1..N]=batch, [N+1]=follow-up probe -- each checks out
    # exactly once, so each needs its own untouched cart.
    customer_ids = [uuid7() for _ in range(_N_TRIP_CALLS + 2)]
    await _grant_customer_roles(customer_ids)
    await _seed_catalog(org_id=org_id, product_id=product_id, sku_id=sku_id, price_minor=1000)
    await _seed_stock(org_id=org_id, product_id=product_id, sku_id=sku_id, on_hand=_STOCK)
    await _seed_carts(
        [(uuid4(), customer_id, org_id, sku_id, product_id, 1) for customer_id in customer_ids]
    )

    idp = FakeIdP(client_scopes={"sf-svc-orders": _ORDERS_SCOPE})
    base = {svc: f"http://127.0.0.1:{port}" for svc, port in _PORTS.items()}
    proxy = await toxiproxy.create_proxy(
        "orders-to-inventory",
        listen_port=_TOXIPROXY_LISTEN_PORT,
        upstream_port=_PORTS["inventory-svc"],
    )
    processes = [
        ServiceProcess(
            "catalog-svc",
            port=_PORTS["catalog-svc"],
            label="fs3-catalog",
            env=service_env("catalog-svc", idp),
        ),
        ServiceProcess(
            "cart-svc",
            port=_PORTS["cart-svc"],
            label="fs3-cart",
            env=service_env(
                "cart-svc",
                idp,
                SF_CATALOG_SVC_BASE_URL=base["catalog-svc"],
                SF_SVC_CART_CLIENT_SECRET="unused",
            ),
        ),
        ServiceProcess(
            "inventory-svc",
            port=_PORTS["inventory-svc"],
            label="fs3-inventory",
            env=service_env("inventory-svc", idp),
        ),
        ServiceProcess(
            "orders-svc",
            port=_PORTS["orders-svc"],
            label="fs3-orders",
            env=service_env(
                "orders-svc",
                idp,
                SF_CART_SVC_BASE_URL=base["cart-svc"],
                SF_CATALOG_SVC_BASE_URL=base["catalog-svc"],
                # The one proxied hop -- see the module docstring.
                SF_INVENTORY_SVC_BASE_URL=proxy.base_url,
                SF_PAYMENTS_SVC_BASE_URL="http://127.0.0.1:1",  # unused by this file's tests
                SF_SVC_ORDERS_CLIENT_SECRET="unused",
            ),
        ),
    ]
    try:
        await asyncio.gather(*(p.start() for p in processes))
        yield Fixture(
            base=base,
            idp=idp,
            toxiproxy=toxiproxy,
            org_id=org_id,
            product_id=product_id,
            sku_id=sku_id,
            customer_ids=customer_ids,
        )
    finally:
        for p in processes:
            p.stop()
        idp.stop()
        await toxiproxy.delete_proxy("orders-to-inventory")
        await toxiproxy.aclose()


async def _checkout(
    http: httpx.AsyncClient, fixture: Fixture, *, customer_id: UUID
) -> httpx.Response:
    token = fixture.idp.user_token(customer_id, email=f"{customer_id}@example.com")
    return await http.post(
        f"{fixture.base['orders-svc']}/v1/stores/{fixture.org_id}/checkouts",
        headers={"authorization": f"Bearer {token}", "idempotency-key": str(uuid4())},
        json={
            "cart_version": 1,
            "shipping_address": _ADDRESS,
            "payment_method_token": "pm_mock_success",
        },
    )


async def test_breaker_opens_under_latency_and_then_fails_fast(fixture: Fixture) -> None:
    # Sanity: the proxy is transparent with no toxic active yet.
    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0)) as http:
        baseline = await _checkout(http, fixture, customer_id=fixture.customer_ids[0])
        assert baseline.status_code == 202, baseline.text

    await fixture.toxiproxy.add_toxic(
        "orders-to-inventory",
        toxic_name="latency",
        toxic_type="latency",
        stream="downstream",
        attributes={"latency": _LATENCY_MS, "jitter": 0},
    )
    try:
        # Enough concurrent, all-failing calls to trip the breaker
        # (min_calls=10, 100% failure rate) -- deterministic once every one
        # of them has resolved, no polling needed. Its own client, closed
        # before the follow-up call opens a fresh connection: 15 requests
        # each held open ~5s by orders-svc's own client timeout is exactly
        # the kind of load that makes a *shared, kept-alive* HTTP/1.1
        # connection to the follow-up request flaky to reason about.
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0)) as batch_http:
            responses = await asyncio.gather(
                *(
                    _checkout(batch_http, fixture, customer_id=customer_id)
                    for customer_id in fixture.customer_ids[1 : 1 + _N_TRIP_CALLS]
                ),
                return_exceptions=True,
            )
        for resp in responses:
            # Each either errored client-side or the server answered with
            # some non-2xx once its own 5s client timeout fired -- either
            # way, never a successful reservation.
            if isinstance(resp, httpx.Response):
                assert resp.status_code >= 400, resp.text

        # The breaker must now be open: this call must fail *fast*, well
        # under the toxic's 7s latency, with the typed 503 LLD-13 §2.3
        # promises -- not another multi-second hang. A fresh client/
        # connection, deliberately: this is the one assertion that must
        # not be confused by keep-alive state left over from the batch.
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0)) as follow_up_http:
            started = asyncio.get_running_loop().time()
            tripped = await _checkout(
                follow_up_http, fixture, customer_id=fixture.customer_ids[_N_TRIP_CALLS + 1]
            )
            elapsed = asyncio.get_running_loop().time() - started

        assert elapsed < 1.0, f"breaker should fail fast, took {elapsed:.2f}s"
        assert tripped.status_code == 503, tripped.text
        assert tripped.json()["title"] == "dependency-unavailable"
    finally:
        await fixture.toxiproxy.remove_toxic("orders-to-inventory", "latency")
