"""Integration tests against the local Compose Postgres (`make test-env`). Skip if unreachable."""

from collections.abc import Iterator
from uuid import UUID, uuid4, uuid7

import pytest
from fastapi.testclient import TestClient
from sf_authz import Principal


class _FakeCatalogClient:
    """Stands in for catalog-svc (not running during this service's own
    tests) -- a dict of (org_id, sku_id) -> availability/price."""

    def __init__(self) -> None:
        self.skus: dict[tuple[str, str], dict] = {}

    def add(
        self, org_id: UUID, sku_id: UUID, *, available: bool = True, price_minor: int = 1000
    ) -> None:
        self.skus[(str(org_id), str(sku_id))] = {
            "sku_id": str(sku_id),
            "product_id": str(uuid7()),
            "product_name": "Test product",
            "sku_status": "active" if available else "archived",
            "product_status": "active",
            "price_minor": price_minor,
        }

    async def get_sku(self, org_id: str, sku_id: str):  # noqa: ANN201 (test double)
        from app.catalog_client import CatalogSku

        body = self.skus.get((org_id, sku_id))
        if body is None:
            return None
        return CatalogSku(**body)


@pytest.fixture
def catalog() -> _FakeCatalogClient:
    return _FakeCatalogClient()


@pytest.fixture
def client(catalog: _FakeCatalogClient) -> Iterator[TestClient]:
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
        subject="svc:orders", audiences=("scaleforge-internal",), claims={"scope": "cart:read"}
    )
    for name in ("require_read_cart", "require_update_cart", "require_delete_cart"):
        app.dependency_overrides[getattr(routes, name)] = lambda principal=principal: principal
    app.dependency_overrides[routes.require_internal_cart_read] = lambda: internal

    try:
        with TestClient(app) as c:
            resp = c.get("/readyz")
            if resp.status_code != 200:
                pytest.skip(f"cart-svc not ready (DB unreachable?): {resp.json()}")
            app.state.catalog_client = catalog
            yield c
    except Exception as exc:
        pytest.skip(f"local Postgres not reachable: {exc}")
    finally:
        app.dependency_overrides.clear()


def _org() -> UUID:
    return uuid4()


def test_get_cart_lazily_creates_an_active_cart(client: TestClient) -> None:
    org_id = _org()

    resp = client.get(f"/v1/me/carts/{org_id}")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["org_id"] == str(org_id)
    assert body["status"] == "active"
    assert body["items"] == []

    # Calling it again returns the same cart, not a second one.
    again = client.get(f"/v1/me/carts/{org_id}")
    assert again.json()["id"] == body["id"]


def test_set_item_validates_the_sku_against_catalog(client: TestClient) -> None:
    org_id = _org()

    resp = client.put(f"/v1/me/carts/{org_id}/items/{uuid7()}", json={"qty": 2})

    assert resp.status_code == 404, resp.text


def test_set_item_rejects_an_unavailable_sku(
    client: TestClient, catalog: _FakeCatalogClient
) -> None:
    org_id, sku_id = _org(), uuid7()
    catalog.add(org_id, sku_id, available=False)

    resp = client.put(f"/v1/me/carts/{org_id}/items/{sku_id}", json={"qty": 1})

    assert resp.status_code == 409, resp.text
    assert resp.json()["title"] == "sku-unavailable"


def test_set_item_is_idempotent_and_overwrites_not_increments(
    client: TestClient, catalog: _FakeCatalogClient
) -> None:
    org_id, sku_id = _org(), uuid7()
    catalog.add(org_id, sku_id)

    client.put(f"/v1/me/carts/{org_id}/items/{sku_id}", json={"qty": 5})
    resp = client.put(f"/v1/me/carts/{org_id}/items/{sku_id}", json={"qty": 3})

    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["qty"] == 3


def test_set_item_rejects_a_mass_assignment_attempt(client: TestClient) -> None:
    org_id = _org()

    resp = client.put(f"/v1/me/carts/{org_id}/items/{uuid7()}", json={"qty": 1, "price_minor": 1})

    assert resp.status_code == 422, resp.text


@pytest.mark.parametrize("qty", [0, 100])
def test_set_item_rejects_qty_outside_1_to_99(client: TestClient, qty: int) -> None:
    resp = client.put(f"/v1/me/carts/{_org()}/items/{uuid7()}", json={"qty": qty})

    assert resp.status_code == 422, resp.text


def test_cart_full_at_51st_distinct_line(client: TestClient, catalog: _FakeCatalogClient) -> None:
    org_id = _org()
    skus = [uuid7() for _ in range(51)]
    for sku_id in skus:
        catalog.add(org_id, sku_id)

    for sku_id in skus[:50]:
        resp = client.put(f"/v1/me/carts/{org_id}/items/{sku_id}", json={"qty": 1})
        assert resp.status_code == 200, resp.text

    resp = client.put(f"/v1/me/carts/{org_id}/items/{skus[50]}", json={"qty": 1})

    assert resp.status_code == 409, resp.text
    assert resp.json()["title"] == "cart-full"


def test_delete_item_is_idempotent(client: TestClient, catalog: _FakeCatalogClient) -> None:
    org_id, sku_id = _org(), uuid7()
    catalog.add(org_id, sku_id)
    client.put(f"/v1/me/carts/{org_id}/items/{sku_id}", json={"qty": 1})

    first = client.delete(f"/v1/me/carts/{org_id}/items/{sku_id}")
    second = client.delete(f"/v1/me/carts/{org_id}/items/{sku_id}")

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["items"] == []
    assert second.json()["items"] == []


def test_delete_item_on_a_cart_that_was_never_created_is_still_success(client: TestClient) -> None:
    resp = client.delete(f"/v1/me/carts/{_org()}/items/{uuid7()}")

    assert resp.status_code == 200, resp.text


def test_clear_removes_every_line(client: TestClient, catalog: _FakeCatalogClient) -> None:
    org_id = _org()
    for _ in range(3):
        sku_id = uuid7()
        catalog.add(org_id, sku_id)
        client.put(f"/v1/me/carts/{org_id}/items/{sku_id}", json={"qty": 1})

    resp = client.delete(f"/v1/me/carts/{org_id}/items")

    assert resp.status_code == 200, resp.text
    assert resp.json()["items"] == []


def test_internal_snapshot_returns_items_and_version(
    client: TestClient, catalog: _FakeCatalogClient
) -> None:
    org_id, sku_id = _org(), uuid7()
    catalog.add(org_id, sku_id)
    cart = client.put(f"/v1/me/carts/{org_id}/items/{sku_id}", json={"qty": 4}).json()

    resp = client.get(f"/internal/v1/carts/{cart['id']}:snapshot")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["version"] == cart["version"]
    assert [i["sku_id"] for i in body["items"]] == [str(sku_id)]


def test_internal_snapshot_404s_for_an_unknown_cart(client: TestClient) -> None:
    resp = client.get(f"/internal/v1/carts/{uuid7()}:snapshot")

    assert resp.status_code == 404
