"""M3 exit evidence: end-to-end commerce through a real browser (LLD-14 §10,
LLD-15 §10 "not built" line).

    uv run --all-packages pytest tests/system/test_checkout_browser.py -q

Real users-svc, catalog-svc, cart-svc, inventory-svc, orders-svc,
payments-svc, mock-payment-provider, bff (now serving apps/web's own build
output, LLD-15 §11 "as built"), and the real Compose Keycloak. A genuine
browser (Playwright/Chromium) drives login, product browse, add-to-cart,
and checkout the way a shopper would -- the one thing every other system
test proves only at the HTTP layer (``keycloak.py``'s own ``Browser`` fakes
rendering entirely; a real browser also genuinely enforces cookie
``HttpOnly``/``SameSite``/storage rules no scripted client can accidentally
bypass).

orgs-svc and authz-svc are not started. Storefront reads are
``Public(...)`` (LLD-05 §6, no cross-service org-existence check), and
``Internal``/``Authorize`` both validate against each service's own
JWKS/Casbin copy locally (ADR-0009, LLD-02 §7's ``authz_reader`` role) --
neither needs a live call to either service. The one thing authz-svc
normally does at runtime that this flow depends on -- granting
``role:customer`` on ``user.created`` (LLD-02 §9) -- is reproduced here by
seeding the grant directly once the real login has JIT-provisioned the
user (its id isn't known before then) and nudging every other service's
own ``PolicyWatcher`` over the same redis-cache pub/sub channel authz-svc
would use for real (LLD-02 §8), rather than starting the service that
would otherwise send it.

``sf-bff``'s realm registration has an extra ``http://localhost:18180/*``
redirect URI/web origin for this file specifically (deploy/compose/
keycloak/realm-scaleforge.json) -- unlike every other system test's BFF
processes, a real browser really navigates to whatever ``redirect_uri`` it
was sent, so this one has to be a free port a real process binds to, not
just a value handed to the OIDC flow.
"""

import asyncio
import contextlib
import os
import re
from pathlib import Path
from uuid import UUID, uuid4, uuid7

import asyncpg
import httpx
import pytest
from harness import ENV, TEST_DB, TEST_REDIS_CACHE_URL, ServiceProcess, service_env, wait_until
from keycloak import KC, KeycloakAdmin, KeycloakIdP
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright, expect
from redis.asyncio import Redis

pytestmark = pytest.mark.asyncio(loop_scope="module")

_APP_ORIGIN = "http://localhost:18180"
_CALLBACK = f"{_APP_ORIGIN}/auth/callback"
_WEB_DIST_DIR = Path(__file__).resolve().parents[2] / "apps" / "web" / "dist"

_PORTS = {
    "users-svc": 18171,
    "catalog-svc": 18172,
    "cart-svc": 18173,
    "inventory-svc": 18174,
    "orders-svc": 18175,
    "payments-svc": 18176,
    "mock-payment-provider": 18177,
    "bff": 18180,
}

_SETTLE_TIMEOUT_S = 30.0
_ORG_ID = uuid4()
_PRODUCT_ID = uuid7()
_SKU_ID = uuid7()
_SLUG = f"e2e-browser-{_PRODUCT_ID}"
_PRICE_MINOR = 1999
_STOCK = 5
_PRODUCT_NAME = "E2E Browser Test Product"

_ADDRESS = ["E2E Shopper", "1 Browser Way", "Berlin", "10115", "DE"]

_JWT_SUBSTRING = re.compile(r"eyJ[\w-]{4,}\.eyJ")


async def _db(role: str) -> asyncpg.Connection:
    return await asyncpg.connect(
        host="localhost", database=TEST_DB, user=role, password=ENV["POSTGRES_APP_PASSWORD"]
    )


async def _seed_catalog_and_stock() -> None:
    conn = await _db("svc_catalog")
    try:
        await conn.execute(
            "INSERT INTO store_pricing_settings "
            "(org_id, status, currency, shipping_flat_minor, tax_rate_bps, source_version, "
            " updated_at) "
            "VALUES ($1, 'active', 'usd', 0, 0, 1, now())",
            _ORG_ID,
        )
        await conn.execute(
            "INSERT INTO products "
            "(id, org_id, slug, name, description_md, description_html, status, version, "
            " created_at, updated_at) "
            "VALUES ($1, $2, $3, $4, '', '', 'active', 1, now(), now())",
            _PRODUCT_ID,
            _ORG_ID,
            _SLUG,
            _PRODUCT_NAME,
        )
        await conn.execute(
            "INSERT INTO skus "
            "(id, org_id, product_id, sku_code, attributes, price_minor, status, version, "
            " created_at, updated_at) "
            "VALUES ($1, $2, $3, $4, '{}'::jsonb, $5, 'active', 1, now(), now())",
            _SKU_ID,
            _ORG_ID,
            _PRODUCT_ID,
            f"E2E-{_SKU_ID}",
            _PRICE_MINOR,
        )
    finally:
        await conn.close()

    conn = await _db("svc_inventory")
    try:
        await conn.execute(
            "INSERT INTO stock_items "
            "(sku_id, org_id, product_id, on_hand, reserved, low_stock_threshold, version, "
            " updated_at) "
            "VALUES ($1, $2, $3, $4, 0, 0, 1, now())",
            _SKU_ID,
            _ORG_ID,
            _PRODUCT_ID,
            _STOCK,
        )
    finally:
        await conn.close()


async def _sf_uid_by_email(email: str) -> UUID:
    conn = await _db("svc_users")

    async def _lookup() -> UUID | None:
        return await conn.fetchval("SELECT id FROM users WHERE email = $1", email)

    try:
        found: UUID | None = None

        async def _ready() -> bool:
            nonlocal found
            found = await _lookup()
            return found is not None

        await wait_until(_ready, timeout_s=_SETTLE_TIMEOUT_S)
        assert found is not None
        return found
    finally:
        await conn.close()


async def _grant_customer_role(user_id: UUID) -> None:
    """Stands in for authz-svc's own ``user.created`` consumer (LLD-02 §9),
    which isn't running here (module docstring). Bumps ``policy_version``
    and publishes it exactly as ``routes.py:_publish_policy_version`` does,
    so every other service's own ``PolicyWatcher`` reloads in well under a
    second instead of waiting out its 10 s poll fallback (LLD-02 §8)."""
    conn = await _db("svc_authz")
    try:
        await conn.execute(
            "INSERT INTO casbin_rule (ptype, v0, v1, v2) "
            "VALUES ('g', $1, 'role:customer', 'customer')",
            f"user:{user_id}",
        )
        version = await conn.fetchval(
            "UPDATE policy_version SET version = version + 1, updated_at = now() "
            "WHERE id = 1 RETURNING version"
        )
    finally:
        await conn.close()

    redis: Redis = Redis.from_url(TEST_REDIS_CACHE_URL, decode_responses=True)
    try:
        await redis.publish("authz:policy", str(version))
    finally:
        await redis.aclose()


def _bff_env(idp: KeycloakIdP) -> dict[str, str]:
    base = {svc: f"http://127.0.0.1:{port}" for svc, port in _PORTS.items()}
    return service_env(
        "bff",
        idp,
        SF_OIDC_CLIENT_ID="sf-bff",
        SF_OIDC_CLIENT_SECRET=ENV["SF_BFF_CLIENT_SECRET"],
        SF_OIDC_REDIRECT_URI=_CALLBACK,
        SF_APP_ORIGIN=_APP_ORIGIN,
        SF_COOKIE_SECURE="false",  # plain http here; production is always Secure
        SF_SVC_BFF_CLIENT_SECRET=ENV["SF_SVC_BFF_CLIENT_SECRET"],
        SF_USERS_SVC_BASE_URL=base["users-svc"],
        # orgs-svc/authz-svc aren't started (module docstring); any
        # unreachable value is fine -- GET /auth/session degrades their
        # hints to an empty list on a connection failure, same as a real
        # outage (routes_auth.py's own `_session_hints`).
        SF_ORGS_SVC_BASE_URL="http://127.0.0.1:1",
        SF_AUTHZ_SVC_BASE_URL="http://127.0.0.1:1",
        SF_CATALOG_SVC_BASE_URL=base["catalog-svc"],
        SF_SEARCH_SVC_BASE_URL="http://127.0.0.1:1",
        SF_CART_SVC_BASE_URL=base["cart-svc"],
        SF_INVENTORY_SVC_BASE_URL=base["inventory-svc"],
        SF_ORDERS_SVC_BASE_URL=base["orders-svc"],
        SF_REDIS_SESSION_URL="redis://localhost:6379/2",
        SF_SESSION_ENC_KEYS='{"k1": "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="}',
        SF_SESSION_ENC_PRIMARY_KID="k1",
        SF_WEB_DIST_DIR=str(_WEB_DIST_DIR),
    )


def _start_processes(idp: KeycloakIdP) -> list[ServiceProcess]:
    base = {svc: f"http://127.0.0.1:{port}" for svc, port in _PORTS.items()}
    return [
        ServiceProcess(
            "users-svc",
            port=_PORTS["users-svc"],
            label="e2e-users",
            env=service_env("users-svc", idp),
        ),
        ServiceProcess(
            "catalog-svc",
            port=_PORTS["catalog-svc"],
            label="e2e-catalog",
            env=service_env("catalog-svc", idp),
        ),
        ServiceProcess(
            "cart-svc",
            port=_PORTS["cart-svc"],
            label="e2e-cart",
            env=service_env(
                "cart-svc",
                idp,
                SF_CATALOG_SVC_BASE_URL=base["catalog-svc"],
                SF_SVC_CART_CLIENT_SECRET=ENV["SF_SVC_CART_CLIENT_SECRET"],
            ),
        ),
        ServiceProcess(
            "inventory-svc",
            port=_PORTS["inventory-svc"],
            label="e2e-inventory",
            env=service_env("inventory-svc", idp),
        ),
        ServiceProcess(
            "orders-svc",
            port=_PORTS["orders-svc"],
            label="e2e-orders",
            env=service_env(
                "orders-svc",
                idp,
                SF_CART_SVC_BASE_URL=base["cart-svc"],
                SF_CATALOG_SVC_BASE_URL=base["catalog-svc"],
                SF_INVENTORY_SVC_BASE_URL=base["inventory-svc"],
                SF_PAYMENTS_SVC_BASE_URL=base["payments-svc"],
                SF_SVC_ORDERS_CLIENT_SECRET=ENV["SF_SVC_ORDERS_CLIENT_SECRET"],
            ),
        ),
        ServiceProcess(
            "payments-svc",
            port=_PORTS["payments-svc"],
            label="e2e-payments",
            env=service_env(
                "payments-svc",
                idp,
                SF_PROVIDER_BASE_URL=base["mock-payment-provider"],
                SF_WEBHOOK_SECRETS=ENV["SF_PAYMENTS_WEBHOOK_SECRET"],
            ),
        ),
        ServiceProcess(
            "mock-payment-provider",
            port=_PORTS["mock-payment-provider"],
            label="e2e-provider",
            # No DB, no Keycloak (ADR-0008) -- built from scratch, not
            # `service_env()`, same as test_commerce_concurrency.py's own
            # note on this service.
            env={
                **os.environ,
                "SF_SERVICE_NAME": "mock-payment-provider",
                "SF_WEBHOOK_TARGET_URL": f"{base['payments-svc']}/webhooks/payments/mock",
                "SF_WEBHOOK_SECRET": ENV["SF_PAYMENTS_WEBHOOK_SECRET"],
                "SF_DELAY_SCALE": "0.01",
                "OTEL_SDK_DISABLED": "true",
            },
        ),
        ServiceProcess("bff", port=_PORTS["bff"], label="e2e-bff", env=_bff_env(idp)),
    ]


async def test_login_browse_add_to_cart_and_checkout() -> None:
    async with httpx.AsyncClient(timeout=10) as http:
        try:
            (await http.get(f"{KC}/realms/scaleforge")).raise_for_status()
        except httpx.HTTPError as exc:
            pytest.skip(f"Compose Keycloak not reachable: {exc}")
        if not _WEB_DIST_DIR.is_dir():
            pytest.skip(f"apps/web isn't built ({_WEB_DIST_DIR} missing) -- run `pnpm build`")

        await _seed_catalog_and_stock()

        idp = KeycloakIdP()
        kc = KeycloakAdmin(http)
        processes = _start_processes(idp)
        await asyncio.gather(*(p.start() for p in processes))
        try:
            user = await kc.create_user()
            try:
                async with async_playwright() as pw:
                    browser = await pw.chromium.launch()
                    try:
                        page = await browser.new_page()
                        product_url = f"{_APP_ORIGIN}/s/{_ORG_ID}/products/{_SLUG}"

                        await page.goto(
                            f"{_APP_ORIGIN}/auth/login?return_to=/s/{_ORG_ID}/products/{_SLUG}"
                        )
                        await page.fill("#username", user.username)
                        await page.fill("#password", user.password)
                        await page.click("#kc-login")

                        # The real browser just followed a real redirect back to
                        # this exact client-side route -- without
                        # services/bff/app/spa.py's SPA-fallback route (LLD-15
                        # §11), this 404s instead of rendering ProductDetailPage.
                        await expect(page).to_have_url(product_url)
                        await expect(
                            page.get_by_role("heading", name=_PRODUCT_NAME)
                        ).to_be_visible()

                        # No access/refresh/id token anywhere a real browser's
                        # own JS or devtools could read it (LLD-01 §16, LLD-15
                        # §3's storage rule) -- checked against genuine
                        # storage/cookie APIs, not an HTTP-response scan.
                        assert await page.evaluate("Object.keys(localStorage)") == []
                        session_values = await page.evaluate("Object.values(sessionStorage)")
                        assert not any(_JWT_SUBSTRING.search(v) for v in session_values)
                        # `cookies()` returns every cookie in the context's jar,
                        # including Keycloak's own (same `domain=localhost`,
                        # different port -- cookie scoping ignores port per
                        # RFC 6265). Keycloak's own `KEYCLOAK_IDENTITY` cookie
                        # is itself a JWT by Keycloak's own design -- that's
                        # the IdP's business, not this app's, so the JWT-shape
                        # check below is scoped to the BFF's own cookie only.
                        cookies = await page.context.cookies()
                        sf_cookies = [c for c in cookies if c["name"] == "sf_session"]
                        assert len(sf_cookies) == 1, [c["name"] for c in cookies]
                        assert sf_cookies[0]["httpOnly"] is True
                        assert not _JWT_SUBSTRING.search(sf_cookies[0]["value"])

                        sf_uid = await _sf_uid_by_email(f"{user.username}@example.com")
                        await _grant_customer_role(sf_uid)

                        # The grant above is real the moment it's committed,
                        # but cart-svc's own `PolicyWatcher` (LLD-02 §8) still
                        # needs its redis-cache pub/sub message to arrive and
                        # its 0.2s debounce to elapse before its *local*
                        # enforcer copy reflects it -- retry through that
                        # window rather than guessing a fixed sleep.
                        async def _added_to_cart() -> bool:
                            await page.get_by_role("button", name="Add to cart").click()
                            with contextlib.suppress(PlaywrightTimeoutError):
                                await page.get_by_text("Added.").wait_for(timeout=1_000)
                                return True
                            return False

                        await wait_until(_added_to_cart, timeout_s=10.0, interval_s=0.5)

                        await page.goto(f"{_APP_ORIGIN}/s/{_ORG_ID}/cart")
                        await expect(page.get_by_text(_PRODUCT_NAME)).to_be_visible()
                        await page.get_by_role("link", name="Checkout").click()
                        await expect(page).to_have_url(f"{_APP_ORIGIN}/s/{_ORG_ID}/checkout")

                        required_inputs = page.locator("input[required]")
                        for i, value in enumerate(_ADDRESS):
                            await required_inputs.nth(i).fill(value)
                        await page.get_by_role("button", name="Place order").click()

                        await expect(page).to_have_url(
                            re.compile(rf"^{re.escape(_APP_ORIGIN)}/account/orders/.+"),
                            timeout=10_000,
                        )
                        await expect(page.get_by_role("button", name="Cancel order")).to_be_visible(
                            timeout=_SETTLE_TIMEOUT_S * 1000
                        )
                    finally:
                        await browser.close()
            finally:
                await kc.delete_user(user)
        finally:
            for p in processes:
                p.stop()
