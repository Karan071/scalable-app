"""``revoke_role`` must emit ``identity.mfa_group_sync_requested`` with
``required: false`` once a user's last ``requires_mfa`` role is gone
(LLD-01 §9's extension, see events.py's ``emit_mfa_group_sync`` docstring).
Exercises the real HTTP routes + real Casbin enforcer, same style as
test_escalation.py, since the grant/revoke guards are part of what's
under test here (only an actor already holding ``super_user`` can grant
or revoke it)."""

import time
from collections.abc import Iterator
from uuid import UUID, uuid7

import pytest
from fastapi.testclient import TestClient
from helpers import grant_role_directly, override_all_as, reload_enforcer
from sqlalchemy import text


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


async def _mfa_sync_rows_for_user(user_id: UUID) -> list[dict]:
    from app.db import build_authz_engine, build_authz_session_factory
    from app.settings import Settings

    engine = build_authz_engine(Settings())
    factory = build_authz_session_factory(engine)
    async with factory() as session:
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT payload FROM outbox "
                        "WHERE payload->>'type' = 'identity.mfa_group_sync_requested' "
                        "AND payload->'data'->>'user_id' = :user_id "
                        "ORDER BY occurred_at"
                    ),
                    {"user_id": str(user_id)},
                )
            )
            .mappings()
            .all()
        )
    await engine.dispose()
    return [row["payload"] for row in rows]


def test_revoking_the_last_mfa_role_emits_required_false(client: TestClient) -> None:
    import asyncio

    actor_id, target_id = uuid7(), uuid7()
    asyncio.run(
        grant_role_directly(
            user_id=actor_id,
            role_id="super_user",
            domain="platform",
            org_id=None,
            granted_by=actor_id,
        )
    )
    asyncio.run(reload_enforcer(client.app))
    override_all_as(client.app, actor_id, acr=2, auth_time=time.time())

    resp = client.post(
        "/v1/platform/role-assignments",
        json={
            "user_id": str(target_id),
            "role_id": "super_user",
            "reason": "granting for mfa sync revocation test",
        },
    )
    assert resp.status_code == 200, resp.text
    assignment = resp.json()

    resp = client.delete(
        f"/v1/platform/role-assignments/{assignment['id']}",
        headers={"If-Match": str(assignment["version"])},
        params={"reason": "revoking for mfa sync revocation test"},
    )
    assert resp.status_code == 200, resp.text

    rows = asyncio.run(_mfa_sync_rows_for_user(target_id))
    required_values = [row["data"]["required"] for row in rows]
    assert required_values == [True, False]
