"""Integration tests against local Compose Postgres + redis-cache. Skips if unreachable."""

import asyncio
from collections.abc import Iterator
from uuid import UUID, uuid4, uuid7

import pytest
from fastapi.testclient import TestClient
from helpers import FakeOrgsClient, grant_role_directly, override_all_as, reload_enforcer


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
                pytest.skip(f"authz-svc not ready (DB/redis unreachable?): {resp.json()}")
            yield c
    except Exception as exc:
        pytest.skip(f"local Postgres/redis not reachable: {exc}")
    finally:
        app.dependency_overrides.clear()


def _setup_org_admin(client: TestClient, *, org_id: UUID, actor_id: UUID) -> None:
    asyncio.run(
        grant_role_directly(
            user_id=actor_id,
            role_id="org_admin",
            domain=f"org:{org_id}",
            org_id=org_id,
            granted_by=actor_id,
        )
    )
    asyncio.run(reload_enforcer(client.app))


def test_assign_org_role_happy_path(client: TestClient) -> None:
    org_id, actor_id, target_id, membership_id = uuid7(), uuid7(), uuid7(), uuid7()
    _setup_org_admin(client, org_id=org_id, actor_id=actor_id)
    override_all_as(client.app, actor_id, acr=2)
    client.app.state.orgs_client = FakeOrgsClient(default_user_id=target_id)

    resp = client.post(
        f"/v1/orgs/{org_id}/role-assignments",
        json={
            "user_id": str(target_id),
            "role_id": "catalog_manager",
            "membership_id": str(membership_id),
        },
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["role_id"] == "catalog_manager"
    assert body["status"] == "active"


def test_assign_org_role_rejects_inactive_membership(client: TestClient) -> None:
    org_id, actor_id, target_id, membership_id = uuid7(), uuid7(), uuid7(), uuid7()
    _setup_org_admin(client, org_id=org_id, actor_id=actor_id)
    override_all_as(client.app, actor_id, acr=2)
    client.app.state.orgs_client = FakeOrgsClient(
        default_user_id=target_id, overrides={str(membership_id): (target_id, "disabled")}
    )

    resp = client.post(
        f"/v1/orgs/{org_id}/role-assignments",
        json={
            "user_id": str(target_id),
            "role_id": "catalog_manager",
            "membership_id": str(membership_id),
        },
    )

    assert resp.status_code == 409


def test_assign_org_role_rejects_role_scope_mismatch(client: TestClient) -> None:
    org_id, actor_id, target_id, membership_id = uuid7(), uuid7(), uuid7(), uuid7()
    _setup_org_admin(client, org_id=org_id, actor_id=actor_id)
    override_all_as(client.app, actor_id, acr=2)
    client.app.state.orgs_client = FakeOrgsClient(default_user_id=target_id)

    resp = client.post(
        f"/v1/orgs/{org_id}/role-assignments",
        json={
            "user_id": str(target_id),
            "role_id": "super_user",
            "membership_id": str(membership_id),
        },
    )

    assert resp.status_code == 422


def test_revoke_role_version_conflict_returns_412(client: TestClient) -> None:
    org_id, actor_id, target_id, membership_id = uuid7(), uuid7(), uuid7(), uuid7()
    _setup_org_admin(client, org_id=org_id, actor_id=actor_id)
    override_all_as(client.app, actor_id, acr=2)
    client.app.state.orgs_client = FakeOrgsClient(default_user_id=target_id)

    created = client.post(
        f"/v1/orgs/{org_id}/role-assignments",
        json={
            "user_id": str(target_id),
            "role_id": "catalog_manager",
            "membership_id": str(membership_id),
        },
    ).json()

    resp = client.request(
        "DELETE",
        f"/v1/orgs/{org_id}/role-assignments/{created['id']}",
        headers={"If-Match": "999"},
    )

    assert resp.status_code == 412


def test_revoke_role_happy_path(client: TestClient) -> None:
    org_id, actor_id, target_id, membership_id = uuid7(), uuid7(), uuid7(), uuid7()
    _setup_org_admin(client, org_id=org_id, actor_id=actor_id)
    override_all_as(client.app, actor_id, acr=2)
    client.app.state.orgs_client = FakeOrgsClient(default_user_id=target_id)

    created = client.post(
        f"/v1/orgs/{org_id}/role-assignments",
        json={
            "user_id": str(target_id),
            "role_id": "catalog_manager",
            "membership_id": str(membership_id),
        },
    ).json()

    resp = client.request(
        "DELETE",
        f"/v1/orgs/{org_id}/role-assignments/{created['id']}",
        headers={"If-Match": str(created["version"])},
    )

    assert resp.status_code == 200
    assert resp.json()["status"] == "revoked"


def test_list_roles_is_self_scoped(client: TestClient) -> None:
    override_all_as(client.app, uuid4(), acr=0)

    resp = client.get("/v1/roles")

    assert resp.status_code == 200
    role_ids = {r["id"] for r in resp.json()}
    assert "org_admin" in role_ids
    assert "super_user" in role_ids


def test_my_role_assignments_spans_every_domain(client: TestClient) -> None:
    org_id, actor_id = uuid7(), uuid7()
    asyncio.run(
        grant_role_directly(
            user_id=actor_id,
            role_id="org_admin",
            domain=f"org:{org_id}",
            org_id=org_id,
            granted_by=actor_id,
        )
    )
    asyncio.run(
        grant_role_directly(
            user_id=actor_id,
            role_id="super_user",
            domain="platform",
            org_id=None,
            granted_by=actor_id,
        )
    )
    override_all_as(client.app, actor_id, acr=0)

    resp = client.get("/v1/me/role-assignments")

    assert resp.status_code == 200
    by_domain = {(a["domain"], a["role_id"]) for a in resp.json()}
    assert (f"org:{org_id}", "org_admin") in by_domain
    assert ("platform", "super_user") in by_domain


def test_my_role_assignments_excludes_other_users(client: TestClient) -> None:
    actor_id, other_id = uuid7(), uuid7()
    asyncio.run(
        grant_role_directly(
            user_id=other_id,
            role_id="super_user",
            domain="platform",
            org_id=None,
            granted_by=other_id,
        )
    )
    override_all_as(client.app, actor_id, acr=0)

    resp = client.get("/v1/me/role-assignments")

    assert resp.status_code == 200
    assert resp.json() == []


def test_assign_org_role_ignores_mass_assignment_fields(client: TestClient) -> None:
    """LLD-14 §4 privilege escalation: AssignRoleRequest declares only
    user_id/role_id/membership_id/reason -- extra fields an attacker might
    hope flow straight into the entity (a pre-set status, a spoofed
    granted_by, a version bump to dodge optimistic-concurrency checks
    elsewhere) must have zero effect."""
    org_id, actor_id, target_id, membership_id = uuid7(), uuid7(), uuid7(), uuid7()
    attacker_claimed_grantor = uuid7()
    _setup_org_admin(client, org_id=org_id, actor_id=actor_id)
    override_all_as(client.app, actor_id, acr=2)
    client.app.state.orgs_client = FakeOrgsClient(default_user_id=target_id)

    resp = client.post(
        f"/v1/orgs/{org_id}/role-assignments",
        json={
            "user_id": str(target_id),
            "role_id": "catalog_manager",
            "membership_id": str(membership_id),
            # None of these are declared on AssignRoleRequest.
            "status": "suspended",
            "granted_by": str(attacker_claimed_grantor),
            "version": 99,
        },
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "active"  # not "suspended" from the extra field
    assert body["granted_by"] == str(actor_id)  # the real actor, not the spoofed one
    assert body["version"] == 1  # not 99


def test_assigning_a_role_the_user_already_holds_is_409_not_500(client: TestClient) -> None:
    """A repeat grant (double-submit, or two admins at once) violates
    uq_role_assignments_active. That's a client-visible conflict, not a
    server error."""
    org_id, actor_id, target_id, membership_id = uuid7(), uuid7(), uuid7(), uuid7()
    _setup_org_admin(client, org_id=org_id, actor_id=actor_id)
    override_all_as(client.app, actor_id, acr=2)
    client.app.state.orgs_client = FakeOrgsClient(default_user_id=target_id)
    body = {
        "user_id": str(target_id),
        "role_id": "catalog_manager",
        "membership_id": str(membership_id),
    }

    first = client.post(f"/v1/orgs/{org_id}/role-assignments", json=body)
    second = client.post(f"/v1/orgs/{org_id}/role-assignments", json=body)

    assert first.status_code == 200, first.text
    assert second.status_code == 409, second.text
    assert second.headers["content-type"] == "application/problem+json"
    # The rejected attempt left nothing behind: one assignment, one grant.
    listed = client.get(f"/v1/orgs/{org_id}/role-assignments", params={"user_id": str(target_id)})
    assert [a["role_id"] for a in listed.json()] == ["catalog_manager"]
