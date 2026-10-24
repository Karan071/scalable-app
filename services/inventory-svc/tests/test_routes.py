"""Integration tests against the local Compose Postgres (`make test-env`). Skip
if unreachable. Covers LLD-07 §4 (reserve/commit/release/adjust) at the HTTP
layer; see test_concurrency.py for the oversell/deadlock/race cases.
"""

import asyncio
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import UUID, uuid4, uuid7

import pytest
from fastapi.testclient import TestClient
from sf_authz import Principal
from sqlalchemy.ext.asyncio import create_async_engine


@pytest.fixture
def client() -> Iterator[TestClient]:
    try:
        from app.main import app
    except Exception as exc:
        pytest.skip(f"could not import app.main: {exc}")

    from app import routes

    internal = Principal(
        subject="svc:orders",
        audiences=("scaleforge-internal",),
        claims={"scope": "inventory:reservations:write inventory:reservations:read"},
    )
    admin = Principal(
        subject="user:00000000-0000-0000-0000-0000000000a1",
        audiences=("scaleforge-api",),
        claims={"sf_uid": "00000000-0000-0000-0000-0000000000a1"},
    )
    app.dependency_overrides[routes.require_reservations_write] = lambda: internal
    app.dependency_overrides[routes.require_reservations_read] = lambda: internal
    for name in (
        "require_stock_read",
        "require_stock_adjust",
        "require_reservation_read",
        "require_reservation_release",
    ):
        app.dependency_overrides[getattr(routes, name)] = lambda principal=admin: principal

    try:
        with TestClient(app) as c:
            resp = c.get("/readyz")
            if resp.status_code != 200:
                pytest.skip(f"inventory-svc not ready (DB unreachable?): {resp.json()}")
            yield c
    except Exception as exc:
        pytest.skip(f"local Postgres not reachable: {exc}")
    finally:
        app.dependency_overrides.clear()


async def _seed_stock(*, org_id: UUID, sku_id: UUID, on_hand: int) -> UUID:
    """A dedicated engine/connection, not ``app.state.session_factory``'s --
    that one is bound to the TestClient's own event loop, and this helper
    runs under a fresh one via ``asyncio.run`` (same reasoning as
    test_tenant_isolation.py's ``_grant_inventory_manager``)."""
    from app.models import StockItem

    engine = create_async_engine(
        f"postgresql+asyncpg://svc_inventory:{os.environ['SF_DB_PASSWORD']}"
        f"@{os.environ['SF_DB_HOST']}:{os.environ['SF_DB_PORT']}/{os.environ['SF_DB_NAME']}"
    )
    product_id = uuid7()
    async with engine.connect() as conn:
        from sqlalchemy.ext.asyncio import AsyncSession

        async with AsyncSession(bind=conn) as session:
            session.add(
                StockItem(
                    sku_id=sku_id,
                    org_id=org_id,
                    product_id=product_id,
                    on_hand=on_hand,
                    reserved=0,
                    low_stock_threshold=0,
                    version=1,
                    updated_at=datetime.now(UTC),
                )
            )
            await session.commit()
    await engine.dispose()
    return product_id


def _seed(client: TestClient, *, org_id: UUID, sku_id: UUID, on_hand: int) -> UUID:
    return asyncio.run(_seed_stock(org_id=org_id, sku_id=sku_id, on_hand=on_hand))


def test_reserve_then_commit_moves_from_reserved_to_sold(client: TestClient) -> None:
    org_id, sku_id, order_id = uuid4(), uuid7(), uuid7()
    _seed(client, org_id=org_id, sku_id=sku_id, on_hand=10)

    resp = client.post(
        "/internal/v1/reservations",
        json={
            "order_id": str(order_id),
            "org_id": str(org_id),
            "lines": [{"sku_id": str(sku_id), "qty": 3}],
        },
    )
    assert resp.status_code == 201, resp.text

    commit_resp = client.post(f"/internal/v1/reservations/{order_id}:commit")
    assert commit_resp.status_code == 200, commit_resp.text
    assert commit_resp.json()["status"] == "committed"

    stock = client.get(f"/v1/orgs/{org_id}/stock").json()["items"]
    item = next(i for i in stock if i["sku_id"] == str(sku_id))
    assert item["on_hand"] == 7
    assert item["reserved"] == 0


def test_reserve_insufficient_stock_is_409(client: TestClient) -> None:
    org_id, sku_id, order_id = uuid4(), uuid7(), uuid7()
    _seed(client, org_id=org_id, sku_id=sku_id, on_hand=1)

    resp = client.post(
        "/internal/v1/reservations",
        json={
            "order_id": str(order_id),
            "org_id": str(org_id),
            "lines": [{"sku_id": str(sku_id), "qty": 5}],
        },
    )

    assert resp.status_code == 409, resp.text
    assert resp.json()["title"] == "insufficient-stock"


def test_reserve_replay_is_idempotent_even_after_stock_changes(client: TestClient) -> None:
    org_id, sku_id, order_id = uuid4(), uuid7(), uuid7()
    _seed(client, org_id=org_id, sku_id=sku_id, on_hand=1)

    first = client.post(
        "/internal/v1/reservations",
        json={
            "order_id": str(order_id),
            "org_id": str(org_id),
            "lines": [{"sku_id": str(sku_id), "qty": 5}],
        },
    )
    assert first.status_code == 409, first.text

    # Stock "arrives" after the rejection -- the replay must still 409.
    client.post(
        f"/v1/orgs/{org_id}/stock/{sku_id}:adjust",
        headers={"If-Match": '"v1"'},
        json={"adjustment_id": str(uuid7()), "delta": 100, "reason": "restock"},
    )

    replay = client.post(
        "/internal/v1/reservations",
        json={
            "order_id": str(order_id),
            "org_id": str(org_id),
            "lines": [{"sku_id": str(sku_id), "qty": 5}],
        },
    )
    assert replay.status_code == 409, replay.text


def test_release_of_an_unknown_order_leaves_a_tombstone_refusing_a_later_reserve(
    client: TestClient,
) -> None:
    org_id, sku_id, order_id = uuid4(), uuid7(), uuid7()
    _seed(client, org_id=org_id, sku_id=sku_id, on_hand=10)

    release_resp = client.post(
        f"/internal/v1/reservations/{order_id}:release", json={"org_id": str(org_id)}
    )
    assert release_resp.status_code == 200, release_resp.text
    assert release_resp.json()["status"] == "released"

    late_reserve = client.post(
        "/internal/v1/reservations",
        json={
            "order_id": str(order_id),
            "org_id": str(org_id),
            "lines": [{"sku_id": str(sku_id), "qty": 1}],
        },
    )
    # The tombstone is a *replay* of the 'released' status, not a fresh reserve.
    assert late_reserve.status_code == 201, late_reserve.text
    stock = client.get(f"/v1/orgs/{org_id}/stock").json()["items"]
    item = next(i for i in stock if i["sku_id"] == str(sku_id))
    assert item["reserved"] == 0  # the tombstone replay never actually reserved


def test_release_after_commit_is_refused(client: TestClient) -> None:
    org_id, sku_id, order_id = uuid4(), uuid7(), uuid7()
    _seed(client, org_id=org_id, sku_id=sku_id, on_hand=10)
    client.post(
        "/internal/v1/reservations",
        json={
            "order_id": str(order_id),
            "org_id": str(org_id),
            "lines": [{"sku_id": str(sku_id), "qty": 1}],
        },
    )
    client.post(f"/internal/v1/reservations/{order_id}:commit")

    resp = client.post(
        f"/internal/v1/reservations/{order_id}:release", json={"org_id": str(org_id)}
    )

    assert resp.status_code == 409, resp.text
    assert resp.json()["title"] == "reservation-committed"


def test_adjust_stale_if_match_is_412(client: TestClient) -> None:
    org_id, sku_id = uuid4(), uuid7()
    _seed(client, org_id=org_id, sku_id=sku_id, on_hand=10)

    resp = client.post(
        f"/v1/orgs/{org_id}/stock/{sku_id}:adjust",
        headers={"If-Match": '"v99"'},
        json={"adjustment_id": str(uuid7()), "delta": 5, "reason": "count"},
    )

    assert resp.status_code == 412, resp.text


def test_adjust_below_reserved_is_409(client: TestClient) -> None:
    org_id, sku_id, order_id = uuid4(), uuid7(), uuid7()
    _seed(client, org_id=org_id, sku_id=sku_id, on_hand=10)
    client.post(
        "/internal/v1/reservations",
        json={
            "order_id": str(order_id),
            "org_id": str(org_id),
            "lines": [{"sku_id": str(sku_id), "qty": 8}],
        },
    )
    stock = client.get(f"/v1/orgs/{org_id}/stock").json()["items"]
    current_version = next(i for i in stock if i["sku_id"] == str(sku_id))["version"]

    resp = client.post(
        f"/v1/orgs/{org_id}/stock/{sku_id}:adjust",
        headers={"If-Match": f'"v{current_version}"'},
        json={"adjustment_id": str(uuid7()), "set_on_hand": 2, "reason": "recount"},
    )

    assert resp.status_code == 409, resp.text
    assert resp.json()["title"] == "below-reserved"


def test_availability_reflects_in_stock_and_low_stock(client: TestClient) -> None:
    org_id, sku_id = uuid4(), uuid7()
    _seed(client, org_id=org_id, sku_id=sku_id, on_hand=10)

    resp = client.get(f"/v1/stores/{org_id}/availability", params={"sku_ids": [str(sku_id)]})

    assert resp.status_code == 200, resp.text
    item = resp.json()["items"][0]
    assert item["in_stock"] is True
    assert item["low_stock"] is False


def test_availability_for_an_unknown_sku_is_out_of_stock(client: TestClient) -> None:
    org_id = uuid4()
    resp = client.get(f"/v1/stores/{org_id}/availability", params={"sku_ids": [str(uuid7())]})

    assert resp.status_code == 200, resp.text
    assert resp.json()["items"][0]["in_stock"] is False


def test_manual_release_within_deadline_refused_without_force(client: TestClient) -> None:
    org_id, sku_id, order_id = uuid4(), uuid7(), uuid7()
    _seed(client, org_id=org_id, sku_id=sku_id, on_hand=10)
    reserved = client.post(
        "/internal/v1/reservations",
        json={
            "order_id": str(order_id),
            "org_id": str(org_id),
            "lines": [{"sku_id": str(sku_id), "qty": 1}],
        },
    ).json()

    resp = client.post(
        f"/v1/orgs/{org_id}/reservations/{reserved['reservation_id']}:release",
        json={"reason": "customer requested cancel"},
    )

    assert resp.status_code == 409, resp.text
    assert resp.json()["title"] == "within-deadline"


def test_manual_release_with_force_succeeds(client: TestClient) -> None:
    org_id, sku_id, order_id = uuid4(), uuid7(), uuid7()
    _seed(client, org_id=org_id, sku_id=sku_id, on_hand=10)
    reserved = client.post(
        "/internal/v1/reservations",
        json={
            "order_id": str(order_id),
            "org_id": str(org_id),
            "lines": [{"sku_id": str(sku_id), "qty": 1}],
        },
    ).json()

    resp = client.post(
        f"/v1/orgs/{org_id}/reservations/{reserved['reservation_id']}:release",
        json={"reason": "support override", "force": True},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "released"
