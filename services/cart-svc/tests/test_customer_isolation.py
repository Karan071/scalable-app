"""Customer isolation (LLD-06 §2): there's no Casbin resource for "my cart"
vs. "someone else's" -- every route's domain is the fixed string
``"customer"`` (any customer may call these routes at all). The real
boundary is ``repository.py``'s own ``customer_id`` filtering, which this
test exercises directly rather than through Casbin.
"""

from collections.abc import Iterator
from uuid import UUID, uuid4, uuid7

import pytest
from fastapi.testclient import TestClient
from sf_authz import Principal
from test_routes import _FakeCatalogClient


def _principal(sf_uid: UUID) -> Principal:
    return Principal(
        subject=f"user:{sf_uid}", audiences=("scaleforge-api",), claims={"sf_uid": str(sf_uid)}
    )


@pytest.fixture
def catalog() -> _FakeCatalogClient:
    return _FakeCatalogClient()


@pytest.fixture
def client(catalog: _FakeCatalogClient) -> Iterator[TestClient]:
    try:
        from app.main import app
    except Exception as exc:
        pytest.skip(f"could not import app.main: {exc}")

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


def _as(client: TestClient, sf_uid: UUID) -> None:
    from app import routes
    from app.main import app

    principal = _principal(sf_uid)
    for name in ("require_read_cart", "require_update_cart", "require_delete_cart"):
        app.dependency_overrides[getattr(routes, name)] = lambda principal=principal: principal


def test_two_customers_in_the_same_org_get_different_carts_and_never_see_each_others_items(
    client: TestClient, catalog: _FakeCatalogClient
) -> None:
    org_id = uuid4()
    customer_a, customer_b = uuid7(), uuid7()
    sku_a, sku_b = uuid7(), uuid7()
    catalog.add(org_id, sku_a)
    catalog.add(org_id, sku_b)

    _as(client, customer_a)
    cart_a = client.put(f"/v1/me/carts/{org_id}/items/{sku_a}", json={"qty": 1}).json()

    _as(client, customer_b)
    cart_b = client.get(f"/v1/me/carts/{org_id}").json()

    assert cart_b["id"] != cart_a["id"]
    assert cart_b["items"] == []

    client.put(f"/v1/me/carts/{org_id}/items/{sku_b}", json={"qty": 1})

    _as(client, customer_a)
    cart_a_again = client.get(f"/v1/me/carts/{org_id}").json()
    assert cart_a_again["id"] == cart_a["id"]
    assert [i["sku_id"] for i in cart_a_again["items"]] == [str(sku_a)]
