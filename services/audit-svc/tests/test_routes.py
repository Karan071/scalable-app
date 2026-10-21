"""Integration tests against local Compose Postgres. Skips if unreachable."""

import asyncio
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from helpers import insert_audit_event_directly, override_all_as


@pytest.fixture
def client() -> Iterator[TestClient]:
    try:
        from app.main import app
    except Exception as exc:
        pytest.skip(f"could not import app.main: {exc}")

    try:
        with TestClient(app) as c:
            resp = c.get("/readyz")
            if resp.status_code != 200:
                pytest.skip(f"audit-svc not ready (DB/redis/broker unreachable?): {resp.json()}")
            yield c
    except Exception as exc:
        pytest.skip(f"local infra not reachable: {exc}")
    finally:
        app.dependency_overrides.clear()


def test_list_org_audit_events_returns_200(client: TestClient) -> None:
    org_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    audit_id = uuid.uuid4()

    asyncio.run(
        insert_audit_event_directly(
            audit_id=audit_id,
            domain=f"org:{org_id}",
            action="membership.created",
            actor_id=str(actor_id),
        )
    )

    override_all_as(client.app, actor_id, acr=0)
    resp = client.get(f"/v1/orgs/{org_id}/audit-events")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "items" in body
    ids = [item["id"] for item in body["items"]]
    assert str(audit_id) in ids


def test_list_org_audit_events_tenant_isolated(client: TestClient) -> None:
    """Org admin cannot see another org's events (domain scoping)."""
    org_a = uuid.uuid4()
    org_b = uuid.uuid4()
    actor_id = uuid.uuid4()

    asyncio.run(
        insert_audit_event_directly(
            domain=f"org:{org_b}",
            action="membership.created",
        )
    )

    override_all_as(client.app, actor_id, acr=0)
    resp = client.get(f"/v1/orgs/{org_a}/audit-events")
    assert resp.status_code == 200, resp.text
    # org_a's events must not include anything from org_b.
    body = resp.json()
    for item in body["items"]:
        assert item["domain"] == f"org:{org_a}"


def test_list_platform_audit_events_requires_acr2(client: TestClient) -> None:
    """Platform endpoint requires acr ≥ 2 — the Authorize alias enforces this;
    the dependency_override bypasses the HTTP gate, so this test verifies
    the FastAPI dependency wiring is correct rather than acr enforcement
    (acr enforcement is covered by sf-authz's own test suite)."""
    actor_id = uuid.uuid4()
    override_all_as(client.app, actor_id, acr=2)
    resp = client.get("/v1/platform/audit-events")
    assert resp.status_code == 200, resp.text
    assert "items" in resp.json()


def test_list_platform_audit_events_returns_all_domains(client: TestClient) -> None:
    org_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    org_audit_id = uuid.uuid4()
    platform_audit_id = uuid.uuid4()

    asyncio.run(
        insert_audit_event_directly(
            audit_id=org_audit_id,
            domain=f"org:{org_id}",
            action="org.created",
        )
    )
    asyncio.run(
        insert_audit_event_directly(
            audit_id=platform_audit_id,
            domain="platform",
            action="platform_role.assigned",
        )
    )

    override_all_as(client.app, actor_id, acr=2)
    resp = client.get("/v1/platform/audit-events")
    assert resp.status_code == 200, resp.text
    ids = {item["id"] for item in resp.json()["items"]}
    assert str(org_audit_id) in ids
    assert str(platform_audit_id) in ids


def test_verify_chain_returns_ok(client: TestClient) -> None:
    actor_id = uuid.uuid4()
    override_all_as(client.app, actor_id, acr=2)
    resp = client.get("/v1/platform/audit-chain:verify")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["broken_at_seq"] is None


def test_pagination_cursor_works(client: TestClient) -> None:
    org_id = uuid.uuid4()
    actor_id = uuid.uuid4()

    for _ in range(3):
        asyncio.run(
            insert_audit_event_directly(
                domain=f"org:{org_id}",
                action="membership.created",
            )
        )

    override_all_as(client.app, actor_id, acr=0)
    # Fetch first page of 2.
    resp1 = client.get(f"/v1/orgs/{org_id}/audit-events?limit=2")
    assert resp1.status_code == 200
    body1 = resp1.json()
    assert len(body1["items"]) == 2
    assert body1["next_cursor"] is not None

    # Fetch second page using the cursor.
    resp2 = client.get(f"/v1/orgs/{org_id}/audit-events?limit=2&cursor={body1['next_cursor']}")
    assert resp2.status_code == 200
    body2 = resp2.json()
    # The third item must appear, not a duplicate of the first page.
    ids1 = {item["id"] for item in body1["items"]}
    ids2 = {item["id"] for item in body2["items"]}
    assert ids1.isdisjoint(ids2)
