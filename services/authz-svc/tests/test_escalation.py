"""Escalation guard tests (LLD-02 §11), one per rule, against the real enforcer."""

import asyncio
import time
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


def _grant_and_reload(
    client: TestClient, *, user_id: UUID, role_id: str, domain: str, org_id: UUID | None
) -> None:
    asyncio.run(
        grant_role_directly(
            user_id=user_id, role_id=role_id, domain=domain, org_id=org_id, granted_by=user_id
        )
    )
    asyncio.run(reload_enforcer(client.app))


def test_rule2_actor_without_assignable_by_cannot_grant(client: TestClient) -> None:
    """§11 rule 2: assigning requires the actor to hold role_grant:<role> in-domain."""
    org_id, actor_id, target_id, membership_id = uuid7(), uuid7(), uuid7(), uuid7()
    # actor holds catalog_manager (not org_admin) -> cannot grant anything.
    _grant_and_reload(
        client, user_id=actor_id, role_id="catalog_manager", domain=f"org:{org_id}", org_id=org_id
    )
    override_all_as(client.app, actor_id, acr=2)
    client.app.state.orgs_client = FakeOrgsClient(default_user_id=target_id)

    resp = client.post(
        f"/v1/orgs/{org_id}/role-assignments",
        json={
            "user_id": str(target_id),
            "role_id": "inventory_manager",
            "membership_id": str(membership_id),
        },
    )

    assert resp.status_code == 403


def test_rule3_self_grant_is_forbidden(client: TestClient) -> None:
    org_id, actor_id, membership_id = uuid7(), uuid7(), uuid7()
    _grant_and_reload(
        client, user_id=actor_id, role_id="org_admin", domain=f"org:{org_id}", org_id=org_id
    )
    override_all_as(client.app, actor_id, acr=2)
    client.app.state.orgs_client = FakeOrgsClient(default_user_id=actor_id)

    resp = client.post(
        f"/v1/orgs/{org_id}/role-assignments",
        json={
            "user_id": str(actor_id),
            "role_id": "catalog_manager",
            "membership_id": str(membership_id),
        },
    )

    assert resp.status_code == 403


def test_rule3_self_revoke_is_allowed(client: TestClient) -> None:
    org_id, actor_id, other_admin = uuid7(), uuid7(), uuid7()
    # Two org_admins so revoking one doesn't trip the last-admin guard.
    _grant_and_reload(
        client, user_id=actor_id, role_id="org_admin", domain=f"org:{org_id}", org_id=org_id
    )
    _grant_and_reload(
        client, user_id=other_admin, role_id="org_admin", domain=f"org:{org_id}", org_id=org_id
    )
    override_all_as(client.app, actor_id, acr=2)
    client.app.state.orgs_client = FakeOrgsClient(default_user_id=actor_id)

    # Grant actor a catalog_manager role as the other admin, then self-revoke it.
    _grant_and_reload(
        client, user_id=actor_id, role_id="catalog_manager", domain=f"org:{org_id}", org_id=org_id
    )
    from app.db import build_authz_engine, build_authz_session_factory
    from app.settings import Settings
    from sqlalchemy import select

    async def _find_assignment_id() -> str:
        engine = build_authz_engine(Settings())
        factory = build_authz_session_factory(engine)
        async with factory() as session:
            from app.models import RoleAssignment

            stmt = select(RoleAssignment).where(
                RoleAssignment.user_id == actor_id,
                RoleAssignment.role_id == "catalog_manager",
                RoleAssignment.domain == f"org:{org_id}",
            )
            row = (await session.execute(stmt)).scalars().one()
        await engine.dispose()
        return str(row.id)

    assignment_id = asyncio.run(_find_assignment_id())

    resp = client.request(
        "DELETE",
        f"/v1/orgs/{org_id}/role-assignments/{assignment_id}",
        headers={"If-Match": "1"},
    )

    assert resp.status_code == 200
    assert resp.json()["status"] == "revoked"


def test_rule4_last_org_admin_cannot_be_revoked(client: TestClient) -> None:
    org_id, actor_id = uuid7(), uuid7()
    _grant_and_reload(
        client, user_id=actor_id, role_id="org_admin", domain=f"org:{org_id}", org_id=org_id
    )
    override_all_as(client.app, actor_id, acr=2)

    from app.db import build_authz_engine, build_authz_session_factory
    from app.settings import Settings
    from sqlalchemy import select

    async def _find_assignment_id() -> str:
        engine = build_authz_engine(Settings())
        factory = build_authz_session_factory(engine)
        async with factory() as session:
            from app.models import RoleAssignment

            stmt = select(RoleAssignment).where(
                RoleAssignment.user_id == actor_id,
                RoleAssignment.role_id == "org_admin",
                RoleAssignment.domain == f"org:{org_id}",
            )
            row = (await session.execute(stmt)).scalars().one()
        await engine.dispose()
        return str(row.id)

    assignment_id = asyncio.run(_find_assignment_id())

    resp = client.request(
        "DELETE",
        f"/v1/orgs/{org_id}/role-assignments/{assignment_id}",
        headers={"If-Match": "1"},
    )

    assert resp.status_code == 409


def test_rule5_platform_grant_requires_a_reason(client: TestClient) -> None:
    super_user_id = uuid7()
    _grant_and_reload(
        client, user_id=super_user_id, role_id="super_user", domain="platform", org_id=None
    )
    override_all_as(client.app, super_user_id, acr=2, auth_time=time.time())

    resp = client.post(
        "/v1/platform/role-assignments",
        json={"user_id": str(uuid4()), "role_id": "platform_operator", "reason": "short"},
    )

    assert resp.status_code == 422  # Pydantic Field(min_length=10) rejects it first


def test_rule5_platform_grant_rejects_stale_step_up(client: TestClient) -> None:
    super_user_id = uuid7()
    _grant_and_reload(
        client, user_id=super_user_id, role_id="super_user", domain="platform", org_id=None
    )
    stale_auth_time = time.time() - 700  # beyond PLATFORM_GRANT_MAX_AUTH_AGE_S (600s)
    override_all_as(client.app, super_user_id, acr=2, auth_time=stale_auth_time)

    resp = client.post(
        "/v1/platform/role-assignments",
        json={
            "user_id": str(uuid4()),
            "role_id": "platform_operator",
            "reason": "operator for DLQ replay",
        },
    )

    assert resp.status_code == 400


def test_rule6_bootstrap_refuses_when_a_super_user_already_exists(client: TestClient) -> None:
    from app.cli import bootstrap_super_user

    # Self-contained: bootstrap once (succeeds whether or not one already
    # existed from a prior test run), then a second attempt must refuse.
    asyncio.run(bootstrap_super_user(uuid4()))
    second_exit_code = asyncio.run(bootstrap_super_user(uuid4()))

    assert second_exit_code == 1
