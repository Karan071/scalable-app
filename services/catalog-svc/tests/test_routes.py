"""Integration tests against the local Compose Postgres (`make test-env`). Skip if unreachable."""

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4, uuid7

import pytest
from fastapi.testclient import TestClient
from sf_authz import Principal


@pytest.fixture
def client() -> Iterator[TestClient]:
    try:
        from app.main import app
    except Exception as exc:
        pytest.skip(f"could not import app.main: {exc}")

    from app import routes

    principal = Principal(
        subject="user:00000000-0000-0000-0000-000000000001",
        audiences=("scaleforge-api",),
        claims={"sf_uid": "00000000-0000-0000-0000-000000000001"},
    )
    internal = Principal(
        subject="svc:orders", audiences=("scaleforge-internal",), claims={"scope": "catalog:quotes"}
    )
    for name in (
        "require_read_product",
        "require_create_product",
        "require_update_product",
        "require_read_category",
        "require_create_category",
        "require_update_category",
        "require_delete_category",
        "require_read_promotion",
        "require_create_promotion",
        "require_update_promotion",
        "require_delete_promotion",
    ):
        app.dependency_overrides[getattr(routes, name)] = lambda principal=principal: principal
    app.dependency_overrides[routes.require_quote_scope] = lambda: internal
    app.dependency_overrides[routes.require_export_scope] = lambda: internal
    app.dependency_overrides[routes.require_sku_read_scope] = lambda: internal

    try:
        with TestClient(app) as c:
            resp = c.get("/readyz")
            if resp.status_code != 200:
                pytest.skip(f"catalog-svc not ready (DB unreachable?): {resp.json()}")
            yield c
    except Exception as exc:
        pytest.skip(f"local Postgres not reachable: {exc}")
    finally:
        app.dependency_overrides.clear()


async def _set_store_active(
    org_id: UUID, *, tax_rate_bps: int = 0, shipping_flat_minor: int = 0
) -> None:
    from app import repository
    from app.db import build_catalog_engine, build_catalog_session_factory
    from app.settings import Settings
    from sf_db import transaction

    settings = Settings()
    engine = build_catalog_engine(settings)
    factory = build_catalog_session_factory(engine)
    async with transaction(factory) as session:
        await repository.upsert_store_settings(
            session,
            org_id=org_id,
            status="active",
            currency="usd",
            shipping_flat_minor=shipping_flat_minor,
            tax_rate_bps=tax_rate_bps,
            source_version=1,
        )
    await engine.dispose()


def _org_id() -> UUID:
    return uuid4()


def test_create_and_read_a_category(client: TestClient) -> None:
    org_id = _org_id()
    resp = client.post(f"/v1/orgs/{org_id}/categories", json={"name": "Shoes", "slug": "shoes"})
    assert resp.status_code == 200, resp.text
    category = resp.json()
    assert category["path"] == "shoes"

    listed = client.get(f"/v1/orgs/{org_id}/categories")
    assert listed.status_code == 200
    assert [c["id"] for c in listed.json()] == [category["id"]]


def test_category_delete_refuses_while_it_has_products(client: TestClient) -> None:
    org_id = _org_id()
    category_id = client.post(
        f"/v1/orgs/{org_id}/categories", json={"name": "Shoes", "slug": "shoes"}
    ).json()["id"]
    client.post(
        f"/v1/orgs/{org_id}/products",
        json={"slug": "boot", "name": "Boot", "category_id": category_id},
    )

    resp = client.delete(f"/v1/orgs/{org_id}/categories/{category_id}")

    assert resp.status_code == 409, resp.text


def test_create_product_rejects_an_unknown_field(client: TestClient) -> None:
    """Mass-assignment guard (LLD-05 §13): a client-supplied price or status
    on a product create must be rejected outright, not silently dropped."""
    org_id = _org_id()

    resp = client.post(
        f"/v1/orgs/{org_id}/products",
        json={"slug": "hat", "name": "Hat", "status": "active"},
    )

    assert resp.status_code == 422, resp.text


def test_product_lifecycle_create_sku_publish_and_quote(client: TestClient) -> None:
    org_id = _org_id()
    asyncio.run(_set_store_active(org_id, tax_rate_bps=875, shipping_flat_minor=500))

    product = client.post(
        f"/v1/orgs/{org_id}/products",
        json={"slug": "tee", "name": "Tee", "description_md": "**soft**"},
    ).json()
    assert product["status"] == "draft"
    assert product["description_html"] == "<p><strong>soft</strong></p>\n"
    assert product["version"] == 1

    sku_id = str(uuid7())
    sku = client.put(
        f"/v1/orgs/{org_id}/products/{product['id']}/skus/{sku_id}",
        json={"sku_code": "TEE-M", "price_minor": 2500},
    ).json()
    assert sku["version"] == 1

    # A product edit without a published SKU change still needs a fresh
    # version -- the SKU PUT above bumped the parent (LLD-05 §4).
    current = client.get(f"/v1/orgs/{org_id}/products/{product['id']}")
    assert current.json()["version"] == 2

    publish = client.post(
        f"/v1/orgs/{org_id}/products/{product['id']}:publish",
        json={},
        headers={"if-match": f'"v{current.json()["version"]}"'},
    )
    assert publish.status_code == 200, publish.text
    assert publish.json()["status"] == "active"

    quote = client.post(
        f"/internal/v1/orgs/{org_id}/quotes", json={"lines": [{"sku_id": sku_id, "qty": 2}]}
    )
    assert quote.status_code == 200, quote.text
    body = quote.json()
    assert body["valid"] is True
    assert body["subtotal_minor"] == 5000
    assert body["shipping_minor"] == 500
    assert body["total_minor"] == 5000 + 500 + body["tax_minor"]


def test_sku_update_requires_if_match(client: TestClient) -> None:
    org_id = _org_id()
    product = client.post(f"/v1/orgs/{org_id}/products", json={"slug": "cap", "name": "Cap"}).json()
    sku_id = str(uuid7())
    client.put(
        f"/v1/orgs/{org_id}/products/{product['id']}/skus/{sku_id}",
        json={"sku_code": "CAP-1", "price_minor": 1000},
    )

    resp = client.put(
        f"/v1/orgs/{org_id}/products/{product['id']}/skus/{sku_id}",
        json={"sku_code": "CAP-1", "price_minor": 1200},
    )

    assert resp.status_code == 428, resp.text


def test_sku_update_with_a_stale_version_is_412_with_the_current_etag(client: TestClient) -> None:
    org_id = _org_id()
    product = client.post(
        f"/v1/orgs/{org_id}/products", json={"slug": "belt", "name": "Belt"}
    ).json()
    sku_id = str(uuid7())
    client.put(
        f"/v1/orgs/{org_id}/products/{product['id']}/skus/{sku_id}",
        json={"sku_code": "BELT-1", "price_minor": 1000},
    )

    resp = client.put(
        f"/v1/orgs/{org_id}/products/{product['id']}/skus/{sku_id}",
        json={"sku_code": "BELT-1", "price_minor": 1200},
        headers={"if-match": '"v99"'},
    )

    assert resp.status_code == 412, resp.text
    assert resp.headers["etag"] == '"v1"'


def test_quote_rejects_a_sku_from_another_org(client: TestClient) -> None:
    """LLD-05 §13: a quote must never price a SKU that belongs to a
    different org, even when the caller supplies its real id. The sku is
    published (an *active* sku on an *active* product) so "unavailable"
    can only be explained by the org filter -- a quote against a draft
    sku would be unavailable either way and prove nothing about isolation."""
    org_a, org_b = _org_id(), _org_id()
    asyncio.run(_set_store_active(org_a))
    asyncio.run(_set_store_active(org_b))
    product = client.post(f"/v1/orgs/{org_b}/products", json={"slug": "mug", "name": "Mug"}).json()
    sku_id = str(uuid7())
    client.put(
        f"/v1/orgs/{org_b}/products/{product['id']}/skus/{sku_id}",
        json={"sku_code": "MUG-1", "price_minor": 900},
    )
    publish = client.post(
        f"/v1/orgs/{org_b}/products/{product['id']}:publish", json={}, headers={"if-match": '"v2"'}
    )
    assert publish.status_code == 200, publish.text

    # Sanity check: org_b's own quote for its own published sku must price it.
    own_quote = client.post(
        f"/internal/v1/orgs/{org_b}/quotes", json={"lines": [{"sku_id": sku_id, "qty": 1}]}
    )
    assert own_quote.json()["lines"][0]["available"] is True

    resp = client.post(
        f"/internal/v1/orgs/{org_a}/quotes", json={"lines": [{"sku_id": sku_id, "qty": 1}]}
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["valid"] is False
    assert body["lines"][0]["available"] is False
    assert body["total_minor"] == 0


def test_quote_for_a_store_with_no_settings_yet_is_409(client: TestClient) -> None:
    """Before the organization.* projection has caught up (or ever will, for
    an org that was never created through orgs-svc), pricing fails closed."""
    org_id = _org_id()

    resp = client.post(
        f"/internal/v1/orgs/{org_id}/quotes", json={"lines": [{"sku_id": str(uuid7()), "qty": 1}]}
    )

    assert resp.status_code == 409, resp.text


def test_promotion_requires_a_valid_window(client: TestClient) -> None:
    org_id = _org_id()
    now = datetime.now(UTC)

    resp = client.post(
        f"/v1/orgs/{org_id}/promotions",
        json={
            "code": "BAD",
            "kind": "percent",
            "value": 10,
            "starts_at": now.isoformat(),
            "ends_at": (now - timedelta(days=1)).isoformat(),
        },
    )

    assert resp.status_code == 422, resp.text


def test_promotion_applies_to_a_quote(client: TestClient) -> None:
    org_id = _org_id()
    asyncio.run(_set_store_active(org_id, tax_rate_bps=0, shipping_flat_minor=0))
    product = client.post(f"/v1/orgs/{org_id}/products", json={"slug": "bag", "name": "Bag"}).json()
    sku_id = str(uuid7())
    client.put(
        f"/v1/orgs/{org_id}/products/{product['id']}/skus/{sku_id}",
        json={"sku_code": "BAG-1", "price_minor": 10_000},
    )
    client.post(
        f"/v1/orgs/{org_id}/products/{product['id']}:publish",
        json={},
        headers={"if-match": '"v2"'},
    )
    now = datetime.now(UTC)
    client.post(
        f"/v1/orgs/{org_id}/promotions",
        json={
            "code": "TENOFF",
            "kind": "percent",
            "value": 10,
            "starts_at": (now - timedelta(days=1)).isoformat(),
            "ends_at": (now + timedelta(days=1)).isoformat(),
        },
    )

    resp = client.post(
        f"/internal/v1/orgs/{org_id}/quotes",
        json={"lines": [{"sku_id": sku_id, "qty": 1}], "promo_code": "tenoff"},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["discount_minor"] == 1000


def test_export_pages_every_product_regardless_of_status(client: TestClient) -> None:
    """LLD-05 §9 step 1: search-svc's reindex job needs drafts and archived
    products too, so it can tombstone them the same way the live consumer
    would -- not just what the admin list (status=active) would show."""
    org_id = _org_id()
    draft_1 = client.post(
        f"/v1/orgs/{org_id}/products", json={"slug": "draft-1", "name": "Draft"}
    ).json()
    draft_2 = client.post(
        f"/v1/orgs/{org_id}/products", json={"slug": "draft-2", "name": "Draft 2"}
    ).json()

    first_page = client.get(f"/internal/v1/orgs/{org_id}/products:export", params={"limit": 1})
    assert first_page.status_code == 200, first_page.text
    body = first_page.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["status"] == "draft"
    assert body["next_cursor"] is not None

    second_page = client.get(
        f"/internal/v1/orgs/{org_id}/products:export",
        params={"limit": 1, "cursor": body["next_cursor"]},
    ).json()
    assert len(second_page["items"]) == 1
    # A full page always offers a next_cursor (the same "maybe more" convention
    # as the admin list and audit-svc's cursor): the walk only ends once a
    # page comes back short.
    third_page = client.get(
        f"/internal/v1/orgs/{org_id}/products:export",
        params={"limit": 1, "cursor": second_page["next_cursor"]},
    ).json()
    assert third_page["items"] == []
    assert third_page["next_cursor"] is None
    # Both products were paged, newest first (same order as the admin list).
    assert {p["product_id"] for p in [*body["items"], *second_page["items"]]} == {
        draft_1["id"],
        draft_2["id"],
    }


def test_export_is_scoped_to_its_org(client: TestClient) -> None:
    org_a, org_b = _org_id(), _org_id()
    client.post(f"/v1/orgs/{org_b}/products", json={"slug": "other-org", "name": "Other"})

    resp = client.get(f"/internal/v1/orgs/{org_a}/products:export")

    assert resp.json()["items"] == []


def test_sku_lookup_finds_an_active_sku(client: TestClient) -> None:
    org_id = _org_id()
    product = client.post(f"/v1/orgs/{org_id}/products", json={"slug": "cup", "name": "Cup"}).json()
    sku_id = str(uuid7())
    client.put(
        f"/v1/orgs/{org_id}/products/{product['id']}/skus/{sku_id}",
        json={"sku_code": "CUP-1", "price_minor": 400},
    )

    resp = client.get(f"/internal/v1/orgs/{org_id}/skus/{sku_id}")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["product_id"] == product["id"]
    assert body["price_minor"] == 400
    assert body["sku_status"] == "active"
    assert body["product_status"] == "draft"


def test_sku_lookup_404s_for_a_sku_from_another_org(client: TestClient) -> None:
    org_a, org_b = _org_id(), _org_id()
    product = client.post(
        f"/v1/orgs/{org_b}/products", json={"slug": "plate", "name": "Plate"}
    ).json()
    sku_id = str(uuid7())
    client.put(
        f"/v1/orgs/{org_b}/products/{product['id']}/skus/{sku_id}",
        json={"sku_code": "PLATE-1", "price_minor": 400},
    )

    resp = client.get(f"/internal/v1/orgs/{org_a}/skus/{sku_id}")

    assert resp.status_code == 404
