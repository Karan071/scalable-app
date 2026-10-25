"""M3 exit evidence: commerce concurrency (LLD-14 §5, delivery plan E3.8).

    make test-system

Real catalog-svc, cart-svc, inventory-svc, orders-svc, payments-svc, and
mock-payment-provider on the test environment, with the fake IdP -- the
first time the full checkout saga runs through a real session instead of
individual services' own fakes (each service's own test suite already
proves these invariants at its own layer in isolation; this is the
end-to-end version across the real stack).

Every customer/product/cart/stock row is seeded directly over SQL rather
than through each service's own admin API or the real
``user.created`` -> ``role:customer`` event flow (both real but slow, and
need services -- users-svc, authz-svc's own consumer, orgs-svc -- this
test doesn't otherwise exercise). Every service's own Casbin enforcer
does a synchronous ``.load()`` at startup, so seeding everything *before*
any process starts means there's nothing to wait for or reload mid-test.

- **Oversell**: `_N_CUSTOMERS` distinct customers, each with their own
  pre-seeded cart (1 unit of the same SKU), check out concurrently
  against stock `_STOCK`. Exactly `_STOCK` must be accepted and settle to
  `confirmed`; the rest must get a synchronous 409 `out-of-stock` -- never
  a 202 that silently fails later. inventory-svc's own ledger must match
  afterwards.
- **Duplicate checkout**: one customer, one cart, `_N_KEYS` concurrent
  checkout calls with `_N_KEYS` different idempotency keys (never the
  same key twice -- that backstop is orders-svc's own idempotency table,
  already covered by its unit tests). Exactly one must succeed; the
  DB-level partial unique index (``uq_orders_cart_version_active``,
  LLD-06 §3.1 "layer 3") is the actual mechanism under test here, now
  under a real concurrent commit race instead of a sequential
  call-then-call-again (orders-svc's own test suite covers both: see
  ``test_checkout.py``'s ``test_checkout_n_concurrent_keys_...`` for the
  same property with fakes standing in for cart/catalog/inventory/
  payments, proving the DB constraint alone without this file's six real
  processes).

Not covered here (already proven with genuine ``asyncio.gather``
concurrency at each service's own layer -- see those services' own
``test_concurrency.py``): multi-SKU opposite-order reservations and the
expiry-vs-commit race (inventory-svc), and concurrent product edits with
the same ETag (catalog-svc).
"""

import asyncio
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID, uuid4, uuid7

import asyncpg
import httpx
import pytest
import pytest_asyncio
from harness import ENV, TEST_DB, FakeIdP, ServiceProcess, service_env, wait_until

pytestmark = pytest.mark.asyncio(loop_scope="module")

_PORTS = {
    "catalog-svc": 18151,
    "cart-svc": 18152,
    "inventory-svc": 18153,
    "orders-svc": 18154,
    "payments-svc": 18155,
    "mock-payment-provider": 18156,
}
_SETTLE_TIMEOUT_S = 30.0
_CUSTOMER_POOL_SIZE = 80
_N_CUSTOMERS = 50
_STOCK = 20
_N_KEYS = 20

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
    """One connection, one batched statement -- not one connection per
    customer. Postgres's own `max_connections` (100 in this Compose stack,
    already shared with every other idle pool) can't absorb a concurrency
    test's own setup opening dozens of raw connections at once."""
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
            "VALUES ($1, $2, $3, 'Concurrency test product', '', '', 'active', 1, now(), now())",
            product_id,
            org_id,
            f"ct-{product_id}",
        )
        await conn.execute(
            "INSERT INTO skus "
            "(id, org_id, product_id, sku_code, attributes, price_minor, status, version, "
            " created_at, updated_at) "
            "VALUES ($1, $2, $3, $4, '{}'::jsonb, $5, 'active', 1, now(), now())",
            sku_id,
            org_id,
            product_id,
            f"CT-{sku_id}",
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


async def _stock_row(sku_id: UUID) -> tuple[int, int]:
    conn = await _db("svc_inventory")
    try:
        row = await conn.fetchrow(
            "SELECT on_hand, reserved FROM stock_items WHERE sku_id = $1", sku_id
        )
        assert row is not None
        return row["on_hand"], row["reserved"]
    finally:
        await conn.close()


async def _seed_cart(
    *, cart_id: UUID, customer_id: UUID, org_id: UUID, sku_id: UUID, product_id: UUID, qty: int
) -> None:
    await _seed_carts([(cart_id, customer_id, org_id, sku_id, product_id, qty)])


async def _seed_carts(
    carts: list[tuple[UUID, UUID, UUID, UUID, UUID, int]],
) -> None:
    """One connection, batched -- see `_grant_customer_roles`'s own note."""
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


async def _non_cancelled_order_count(customer_id: UUID) -> int:
    conn = await _db("svc_orders")
    try:
        return await conn.fetchval(
            "SELECT count(*) FROM orders WHERE customer_id = $1 AND status <> 'cancelled'",
            customer_id,
        )
    finally:
        await conn.close()


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def customer_pool() -> list[UUID]:
    """Seeded once, before any service starts (see module docstring): enough
    distinct ``role:customer`` grants for every test in this module, so
    nothing needs a live policy reload mid-test."""
    try:
        conn = await _db("svc_authz")
        await conn.close()
    except Exception as exc:
        pytest.skip(f"local Postgres not reachable: {exc}")
    ids = [uuid7() for _ in range(_CUSTOMER_POOL_SIZE)]
    await _grant_customer_roles(ids)
    return ids


@dataclass
class Stack:
    base: dict[str, str]
    #: The same `FakeIdP` the running processes trust -- a test must mint
    #: its tokens from this instance, not a fresh `FakeIdP()` (each one
    #: generates its own RSA key, so a token from any other instance is
    #: just a signature a running service's JWKS can never match).
    idp: FakeIdP


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def stack(customer_pool: list[UUID]) -> AsyncIterator[Stack]:
    idp = FakeIdP(client_scopes={"sf-svc-orders": _ORDERS_SCOPE})
    base = {svc: f"http://127.0.0.1:{port}" for svc, port in _PORTS.items()}
    processes = [
        ServiceProcess(
            "catalog-svc",
            port=_PORTS["catalog-svc"],
            label="cc-catalog",
            env=service_env("catalog-svc", idp),
        ),
        ServiceProcess(
            "cart-svc",
            port=_PORTS["cart-svc"],
            label="cc-cart",
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
            label="cc-inventory",
            env=service_env("inventory-svc", idp),
        ),
        ServiceProcess(
            "orders-svc",
            port=_PORTS["orders-svc"],
            label="cc-orders",
            env=service_env(
                "orders-svc",
                idp,
                SF_CART_SVC_BASE_URL=base["cart-svc"],
                SF_CATALOG_SVC_BASE_URL=base["catalog-svc"],
                SF_INVENTORY_SVC_BASE_URL=base["inventory-svc"],
                SF_PAYMENTS_SVC_BASE_URL=base["payments-svc"],
                SF_SVC_ORDERS_CLIENT_SECRET="unused",
            ),
        ),
        ServiceProcess(
            "payments-svc",
            port=_PORTS["payments-svc"],
            label="cc-payments",
            # payments-svc's own Settings has no authz/redis/invitation/
            # token-endpoint fields at all (no Casbin enforcer, no outbound
            # Keycloak calls -- see its settings.py docstring); service_env()
            # still sets those env vars, but an env var with no matching
            # field is simply never looked up, not a validation error.
            env=service_env(
                "payments-svc",
                idp,
                SF_PROVIDER_BASE_URL=base["mock-payment-provider"],
                SF_WEBHOOK_SECRETS="cc-webhook-secret",
            ),
        ),
        ServiceProcess(
            "mock-payment-provider",
            port=_PORTS["mock-payment-provider"],
            label="cc-provider",
            env={
                **os.environ,
                "SF_SERVICE_NAME": "mock-payment-provider",
                "SF_WEBHOOK_TARGET_URL": f"{base['payments-svc']}/webhooks/payments/mock",
                "SF_WEBHOOK_SECRET": "cc-webhook-secret",
                "SF_DELAY_SCALE": "0.01",
                "OTEL_SDK_DISABLED": "true",
            },
        ),
    ]
    try:
        await asyncio.gather(*(p.start() for p in processes))
        yield Stack(base=base, idp=idp)
    finally:
        for p in processes:
            p.stop()
        idp.stop()


async def _checkout(
    http: httpx.AsyncClient,
    stack: Stack,
    *,
    customer_id: UUID,
    org_id: UUID,
    cart_version: int = 1,
    payment_method_token: str = "pm_mock_success",
) -> httpx.Response:
    token = stack.idp.user_token(customer_id, email=f"{customer_id}@example.com")
    return await http.post(
        f"{stack.base['orders-svc']}/v1/stores/{org_id}/checkouts",
        headers={"authorization": f"Bearer {token}", "idempotency-key": str(uuid4())},
        json={
            "cart_version": cart_version,
            "shipping_address": _ADDRESS,
            "payment_method_token": payment_method_token,
        },
    )


async def _order_status(
    http: httpx.AsyncClient, stack: Stack, *, customer_id: UUID, order_id: str
) -> str:
    token = stack.idp.user_token(customer_id, email=f"{customer_id}@example.com")
    resp = await http.get(
        f"{stack.base['orders-svc']}/v1/me/orders/{order_id}",
        headers={"authorization": f"Bearer {token}"},
    )
    resp.raise_for_status()
    return resp.json()["status"]  # type: ignore[no-any-return]


# --- tests -------------------------------------------------------------------------------


async def test_oversell_exactly_stock_count_succeeds(
    stack: Stack, customer_pool: list[UUID]
) -> None:
    org_id, product_id, sku_id = uuid4(), uuid7(), uuid7()
    await _seed_catalog(org_id=org_id, product_id=product_id, sku_id=sku_id, price_minor=1000)
    await _seed_stock(org_id=org_id, product_id=product_id, sku_id=sku_id, on_hand=_STOCK)

    customers = customer_pool[:_N_CUSTOMERS]
    cart_ids = {cid: uuid7() for cid in customers}
    await _seed_carts(
        [(cart_ids[cid], cid, org_id, sku_id, product_id, 1) for cid in customers]
    )

    async with httpx.AsyncClient(
        timeout=30.0, limits=httpx.Limits(max_connections=200)
    ) as http:
        responses = await asyncio.gather(
            *(_checkout(http, stack, customer_id=cid, org_id=org_id) for cid in customers)
        )

        accepted = [
            (cid, r) for cid, r in zip(customers, responses, strict=True) if r.status_code == 202
        ]
        rejected = [r for r in responses if r.status_code != 202]
        assert len(accepted) == _STOCK, [r.status_code for r in responses]
        assert len(rejected) == _N_CUSTOMERS - _STOCK
        assert all(r.status_code == 409 and r.json()["title"] == "out-of-stock" for r in rejected)

        async def all_confirmed() -> bool:
            statuses = await asyncio.gather(
                *(
                    _order_status(http, stack, customer_id=cid, order_id=r.json()["order_id"])
                    for cid, r in accepted
                )
            )
            return all(s == "confirmed" for s in statuses)

        await wait_until(all_confirmed, timeout_s=_SETTLE_TIMEOUT_S)

    on_hand, reserved = await _stock_row(sku_id)
    assert on_hand == 0  # every unit of stock got committed to exactly one order
    assert reserved == 0


async def test_duplicate_checkout_n_concurrent_keys_same_cart_is_one_order(
    stack: Stack, customer_pool: list[UUID]
) -> None:
    org_id, product_id, sku_id = uuid4(), uuid7(), uuid7()
    customer_id = customer_pool[-1]
    await _seed_catalog(org_id=org_id, product_id=product_id, sku_id=sku_id, price_minor=500)
    await _seed_stock(org_id=org_id, product_id=product_id, sku_id=sku_id, on_hand=1000)
    await _seed_cart(
        cart_id=uuid7(),
        customer_id=customer_id,
        org_id=org_id,
        sku_id=sku_id,
        product_id=product_id,
        qty=1,
    )

    async with httpx.AsyncClient(
        timeout=30.0, limits=httpx.Limits(max_connections=50)
    ) as http:
        responses = await asyncio.gather(
            *(
                _checkout(http, stack, customer_id=customer_id, org_id=org_id)
                for _ in range(_N_KEYS)
            )
        )

    accepted = [r for r in responses if r.status_code == 202]
    rejected = [r for r in responses if r.status_code == 409]
    assert len(accepted) == 1, [r.status_code for r in responses]
    assert len(rejected) == _N_KEYS - 1
    assert all(r.json()["title"] == "cart-changed" for r in rejected)

    assert await _non_cancelled_order_count(customer_id) == 1
