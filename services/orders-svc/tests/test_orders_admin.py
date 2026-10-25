"""Integration tests against the local Compose Postgres (`make test-env`).
Skip if unreachable. LLD-06 §6: the order state machine's admin ops and
customer views, at the HTTP layer.
"""

import asyncio
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import UUID, uuid4, uuid7

import pytest
from app.models import Order, Outbox
from fastapi.testclient import TestClient
from sf_authz import Principal
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine


@pytest.fixture
def client() -> Iterator[TestClient]:
    try:
        from app.main import app
    except Exception as exc:
        pytest.skip(f"could not import app.main: {exc}")

    from app import routes

    customer_id = uuid7()
    admin_id = uuid7()
    customer_principal = Principal(
        subject=f"user:{customer_id}",
        audiences=("scaleforge-api",),
        claims={"sf_uid": str(customer_id)},
    )
    admin_principal = Principal(
        subject=f"user:{admin_id}", audiences=("scaleforge-api",), claims={"sf_uid": str(admin_id)}
    )
    for name in ("require_read_own_order", "require_cancel_own_order"):
        app.dependency_overrides[getattr(routes, name)] = lambda p=customer_principal: p
    for name in (
        "require_read_org_orders",
        "require_update_order_status",
        "require_cancel_org_order",
        "require_refund",
    ):
        app.dependency_overrides[getattr(routes, name)] = lambda p=admin_principal: p

    try:
        with TestClient(app) as c:
            resp = c.get("/readyz")
            if resp.status_code != 200:
                pytest.skip(f"orders-svc not ready (DB unreachable?): {resp.json()}")
            c.customer_id = customer_id  # type: ignore[attr-defined]
            c.admin_id = admin_id  # type: ignore[attr-defined]
            yield c
    except Exception as exc:
        pytest.skip(f"local Postgres not reachable: {exc}")
    finally:
        app.dependency_overrides.clear()


async def _seed_order(
    *, org_id: UUID, customer_id: UUID, status: str = "confirmed", payment_status: str = "captured"
) -> UUID:
    """A dedicated engine, not ``app.state.session_factory``'s -- that one
    is bound to the TestClient's own event loop (same reasoning as
    inventory-svc's test helpers)."""
    order_id = uuid7()
    now = datetime.now(UTC)
    engine = create_async_engine(
        f"postgresql+asyncpg://svc_orders:{os.environ['SF_DB_PASSWORD']}"
        f"@{os.environ['SF_DB_HOST']}:{os.environ['SF_DB_PORT']}/{os.environ['SF_DB_NAME']}"
    )
    async with engine.connect() as conn:
        session = AsyncSession(bind=conn)
        session.add(
            Order(
                id=order_id,
                org_id=org_id,
                customer_id=customer_id,
                status=status,
                payment_status=payment_status,
                cart_id=uuid7(),
                cart_version=1,
                idempotency_key=uuid7(),
                currency="EUR",
                subtotal_minor=1000,
                discount_minor=0,
                shipping_minor=500,
                tax_minor=0,
                total_minor=1500,
                promo_code=None,
                pricing_fingerprint="fake",
                shipping_address={
                    "name": "A",
                    "line1": "1 Main St",
                    "city": "Berlin",
                    "postal_code": "10115",
                    "country": "DE",
                },
                cancel_reason=None,
                version=1,
                created_at=now,
                updated_at=now,
            )
        )
        await session.commit()
    await engine.dispose()
    return order_id


def _seed(client: TestClient, **kwargs) -> UUID:
    return asyncio.run(_seed_order(**kwargs))


def test_list_my_orders_returns_only_my_own(client: TestClient) -> None:
    org_id = uuid4()
    mine = _seed(client, org_id=org_id, customer_id=client.customer_id)
    _seed(client, org_id=org_id, customer_id=uuid7())  # someone else's

    resp = client.get("/v1/me/orders")

    assert resp.status_code == 200, resp.text
    ids = [item["id"] for item in resp.json()["items"]]
    assert str(mine) in ids
    assert len(ids) == 1


def test_get_my_order_404s_for_someone_elses_order(client: TestClient) -> None:
    order_id = _seed(client, org_id=uuid4(), customer_id=uuid7())

    resp = client.get(f"/v1/me/orders/{order_id}")

    assert resp.status_code == 404


def test_cancel_my_order_only_allowed_from_confirmed(client: TestClient) -> None:
    order_id = _seed(client, org_id=uuid4(), customer_id=client.customer_id, status="confirmed")

    resp = client.post(
        f"/v1/me/orders/{order_id}:cancel", headers={"Idempotency-Key": str(uuid7())}
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "cancelled"


def test_cancel_my_order_from_pending_is_refused(client: TestClient) -> None:
    order_id = _seed(client, org_id=uuid4(), customer_id=client.customer_id, status="pending")

    resp = client.post(
        f"/v1/me/orders/{order_id}:cancel", headers={"Idempotency-Key": str(uuid7())}
    )

    assert resp.status_code == 409, resp.text
    assert resp.json()["title"] == "order-not-cancellable"


def test_admin_list_org_orders(client: TestClient) -> None:
    org_id = uuid4()
    _seed(client, org_id=org_id, customer_id=uuid7())
    other_org_order = _seed(client, org_id=uuid4(), customer_id=uuid7())

    resp = client.get(f"/v1/orgs/{org_id}/orders")

    assert resp.status_code == 200, resp.text
    ids = [item["id"] for item in resp.json()["items"]]
    assert str(other_org_order) not in ids


def test_admin_transition_confirmed_to_processing(client: TestClient) -> None:
    org_id = uuid4()
    order_id = _seed(client, org_id=org_id, customer_id=uuid7(), status="confirmed")

    resp = client.post(
        f"/v1/orgs/{org_id}/orders/{order_id}:transition",
        headers={"If-Match": '"v1"'},
        json={"to": "processing"},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "processing"


def test_admin_transition_disallowed_edge_is_409(client: TestClient) -> None:
    org_id = uuid4()
    order_id = _seed(client, org_id=org_id, customer_id=uuid7(), status="confirmed")

    resp = client.post(
        f"/v1/orgs/{org_id}/orders/{order_id}:transition",
        headers={"If-Match": '"v1"'},
        json={"to": "delivered"},
    )

    assert resp.status_code == 409, resp.text
    assert resp.json()["title"] == "invalid-transition"


def test_admin_transition_stale_if_match_is_412(client: TestClient) -> None:
    org_id = uuid4()
    order_id = _seed(client, org_id=org_id, customer_id=uuid7(), status="confirmed")

    resp = client.post(
        f"/v1/orgs/{org_id}/orders/{order_id}:transition",
        headers={"If-Match": '"v99"'},
        json={"to": "processing"},
    )

    assert resp.status_code == 412, resp.text


async def _outbox_routing_keys(order_id: UUID) -> set[str]:
    engine = create_async_engine(
        f"postgresql+asyncpg://svc_orders:{os.environ['SF_DB_PASSWORD']}"
        f"@{os.environ['SF_DB_HOST']}:{os.environ['SF_DB_PORT']}/{os.environ['SF_DB_NAME']}"
    )
    async with engine.connect() as conn:
        session = AsyncSession(bind=conn)
        rows = (
            (await session.execute(select(Outbox).where(Outbox.aggregate_id == str(order_id))))
            .scalars()
            .all()
        )
    await engine.dispose()
    return {row.routing_key for row in rows}


def test_admin_cancel_fires_compensation_commands(client: TestClient) -> None:
    org_id = uuid4()
    order_id = _seed(client, org_id=org_id, customer_id=uuid7(), status="processing")

    resp = client.post(
        f"/v1/orgs/{org_id}/orders/{order_id}:cancel",
        headers={"Idempotency-Key": str(uuid7()), "If-Match": '"v1"'},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "cancelled"
    routing_keys = asyncio.run(_outbox_routing_keys(order_id))
    assert "inventory.restock_requested.v1" in routing_keys
    assert "payment.refund_requested.v1" in routing_keys
    assert "order.cancelled.v1" in routing_keys


def test_admin_refund_requires_delivered_status(client: TestClient) -> None:
    org_id = uuid4()
    order_id = _seed(client, org_id=org_id, customer_id=uuid7(), status="confirmed")

    resp = client.post(
        f"/v1/orgs/{org_id}/orders/{order_id}:refund", headers={"Idempotency-Key": str(uuid7())}
    )

    assert resp.status_code == 409, resp.text
    assert resp.json()["title"] == "order-not-refundable"


def test_admin_refund_delivered_order_fires_refund_command(client: TestClient) -> None:
    org_id = uuid4()
    order_id = _seed(client, org_id=org_id, customer_id=uuid7(), status="delivered")

    resp = client.post(
        f"/v1/orgs/{org_id}/orders/{order_id}:refund", headers={"Idempotency-Key": str(uuid7())}
    )

    assert resp.status_code == 202, resp.text
    assert resp.json()["payment_status"] == "refunding"
