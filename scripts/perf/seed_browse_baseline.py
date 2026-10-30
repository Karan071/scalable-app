"""E4.1 (LLD-14 §6): provisions what the Browse k6 scenario needs against
the isolated test environment (`make test-env`) -- real Keycloak users and
a real catalog, seeded directly over SQL (test_commerce_concurrency.py's
own reasoning: fast, and this is data for a load generator, not a
correctness test). Writes `scripts/perf/.browse-baseline.json` for the k6
script and the service-starting shell commands to read back.

Run once per k6 session, after the needed processes are up:

    uv run python scripts/perf/seed_browse_baseline.py --users 30 --products 40
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

import asyncpg
import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tests" / "system"))

from harness import ENV, TEST_DB  # noqa: E402
from keycloak import KeycloakAdmin  # noqa: E402

CONFIG_PATH = Path(__file__).resolve().parent / ".browse-baseline.json"

_CATEGORY_NAMES = ["electronics", "home", "outdoors", "books"]


async def _db(role: str) -> asyncpg.Connection:
    return await asyncpg.connect(
        host="localhost", database=TEST_DB, user=role, password=ENV["POSTGRES_APP_PASSWORD"]
    )


async def _seed_catalog(*, org_id: str, n_products: int) -> list[str]:
    """Returns the seeded products' slugs."""
    conn = await _db("svc_catalog")
    try:
        await conn.execute(
            "INSERT INTO store_pricing_settings "
            "(org_id, status, currency, shipping_flat_minor, tax_rate_bps, source_version, "
            " updated_at) "
            "VALUES ($1, 'active', 'usd', 0, 0, 1, now())",
            org_id,
        )
        category_ids = [str(uuid4()) for _ in _CATEGORY_NAMES]
        await conn.executemany(
            "INSERT INTO categories (id, org_id, parent_id, path, name, slug, created_at, "
            " updated_at) "
            "VALUES ($1, $2, NULL, $3::ltree, $4, $4, now(), now())",
            [
                (cat_id, org_id, name, name)
                for cat_id, name in zip(category_ids, _CATEGORY_NAMES, strict=True)
            ],
        )

        slugs = [f"perf-browse-{i:04d}-{uuid4().hex[:6]}" for i in range(n_products)]
        product_ids = [str(uuid4()) for _ in range(n_products)]
        await conn.executemany(
            "INSERT INTO products "
            "(id, org_id, slug, name, description_md, description_html, status, "
            " category_id, version, created_at, updated_at) "
            "VALUES ($1, $2, $3, $4, 'Perf baseline product (E4.1)', "
            " '<p>Perf baseline product (E4.1)</p>', 'active', $5, 1, now(), now())",
            [
                (
                    product_ids[i],
                    org_id,
                    slugs[i],
                    f"Perf Baseline Product {i}",
                    category_ids[i % len(category_ids)],
                )
                for i in range(n_products)
            ],
        )
        sku_rows = []
        for i in range(n_products):
            for s in range(1 + i % 3):  # 1-3 SKUs per product
                sku_rows.append(
                    (
                        str(uuid4()),
                        org_id,
                        product_ids[i],
                        f"PERF-{i:04d}-{s}",
                        1000 + (i * 137 + s * 29) % 9000,
                    )
                )
        await conn.executemany(
            "INSERT INTO skus "
            "(id, org_id, product_id, sku_code, attributes, price_minor, status, version, "
            " created_at, updated_at) "
            "VALUES ($1, $2, $3, $4, '{}'::jsonb, $5, 'active', 1, now(), now())",
            sku_rows,
        )

        await _seed_search_docs(
            org_id=org_id,
            product_ids=product_ids,
            slugs=slugs,
            category_ids=category_ids,
            sku_rows=sku_rows,
        )
        return slugs
    finally:
        await conn.close()


async def _seed_search_docs(
    *,
    org_id: str,
    product_ids: list[str],
    slugs: list[str],
    category_ids: list[str],
    sku_rows: list[tuple[str, str, str, str, int]],
) -> None:
    """search-svc's own read model (LLD-05 §7.1) -- seeded directly rather
    than through the real product.updated.v1 -> inbox pipeline (E3.2), the
    same reasoning as catalog-svc's own rows above: this is load-generator
    fixture data, not a test of the indexing pipeline itself."""
    conn = await _db("svc_search")
    try:
        prices_by_product: dict[str, list[int]] = {}
        for _id, _org, product_id, _code, price in sku_rows:
            prices_by_product.setdefault(product_id, []).append(price)
        await conn.executemany(
            "INSERT INTO product_search_docs "
            "(org_id, product_id, slug, name, description_text, category_ids, "
            " min_price_minor, max_price_minor, in_stock, created_at, product_version, "
            " indexed_at) "
            "VALUES ($1, $2, $3, $4, 'Perf baseline product (E4.1)', $5, $6, $7, true, now(), "
            " 1, now())",
            [
                (
                    org_id,
                    product_ids[i],
                    slugs[i],
                    f"Perf Baseline Product {i}",
                    [category_ids[i % len(category_ids)]],
                    min(prices_by_product[product_ids[i]]),
                    max(prices_by_product[product_ids[i]]),
                )
                for i in range(len(product_ids))
            ],
        )
    finally:
        await conn.close()


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--users", type=int, default=30)
    parser.add_argument("--products", type=int, default=40)
    parser.add_argument(
        # localhost:18180, not 127.0.0.1: the one host:port pair, besides
        # localhost:8020, the realm's sf-bff client has a registered
        # redirect URI for (deploy/compose/keycloak/realm-scaleforge.json).
        # Must match exactly -- the BFF's own state cookie, set when this
        # same host:port is asked for /auth/login, has to come back on the
        # callback to the *same* host:port or the BFF can't find it
        # (confirmed directly: 127.0.0.1 vs localhost for one of the two
        # requests is "binding_invalid", not a redirect-URI mismatch).
        "--bff-base-url",
        default=os.environ.get("BROWSE_BFF_BASE_URL", "http://localhost:18180"),
    )
    args = parser.parse_args()

    async with httpx.AsyncClient(timeout=10) as http:
        kc = KeycloakAdmin(http)
        users = []
        for _ in range(args.users):
            user = await kc.create_user()
            users.append({"username": user.username, "password": user.password, "id": user.id})

    org_id = str(uuid4())
    slugs = await _seed_catalog(org_id=org_id, n_products=args.products)

    CONFIG_PATH.write_text(
        json.dumps(
            {
                "base_url": args.bff_base_url,
                "org_id": org_id,
                "slugs": slugs,
                "users": users,
            },
            indent=2,
        )
    )
    print(f"wrote {CONFIG_PATH} -- {len(users)} users, {len(slugs)} products, org {org_id}")


if __name__ == "__main__":
    asyncio.run(main())
