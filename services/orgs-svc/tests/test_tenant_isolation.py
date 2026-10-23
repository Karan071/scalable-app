"""Tenant-isolation matrix (LLD-14 §4): for a representative subset of
org-scoped routes (one read, one mutate -- the full LLD-14 §4 design is "one
route metadata generator for every route," a bigger generic-tooling lift not
attempted here), actors {no membership, a cross-org grant, a suspended
grant} x {own-org path, other-org path} must get 403/404. Zero 2xx.

Unlike every other orgs-svc test file, this one does NOT use
``_as_user()``'s override-every-Authorize-alias helper -- that bypasses
real Casbin enforcement entirely, which is exactly the thing under test
here. Instead it swaps ``app.state.token_validator`` for a fake that maps
bearer-token *strings* straight to ``Principal`` objects, leaving
``Authorize(...)``'s real enforcement (against orgs-svc's own embedded
enforcer, reading the same local Postgres ``authz`` schema authz-svc
writes to) to run genuinely.
"""

import asyncio
import os
from collections.abc import Iterator
from uuid import UUID, uuid4, uuid7

import pytest
from fastapi.testclient import TestClient
from sf_authz import Principal, TokenValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

# Local-dev-only: every svc_* role shares POSTGRES_APP_PASSWORD (init-schemas.sh).
# orgs-svc's own Settings only has read-only authz_reader creds, so granting
# roles for this test needs svc_authz's write access directly.
_AUTHZ_WRITE_DSN = (
    f"postgresql+asyncpg://svc_authz:app_local_dev@localhost:5432/{os.environ['SF_DB_NAME']}"
)
_ORGS_WRITE_DSN = (
    f"postgresql+asyncpg://svc_orgs:app_local_dev@localhost:5432/{os.environ['SF_DB_NAME']}"
)


class FakeTokenValidator:
    def __init__(self) -> None:
        self._principals: dict[str, Principal] = {}

    def register(self, token: str, principal: Principal) -> None:
        self._principals[token] = principal

    def validate(self, token: str, *, audience: str, leeway_s: float = 30.0) -> Principal:
        principal = self._principals.get(token)
        if principal is None:
            raise TokenValidationError("unknown test token")
        return principal


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
                pytest.skip(f"orgs-svc not ready (DB unreachable?): {resp.json()}")
            fake_validator = FakeTokenValidator()
            app.state.token_validator = fake_validator
            c.fake_validator = fake_validator  # type: ignore[attr-defined]
            yield c
    except Exception as exc:
        pytest.skip(f"local Postgres not reachable: {exc}")


def _principal(sf_uid: UUID) -> Principal:
    return Principal(
        subject=f"user:{sf_uid}",
        audiences=("scaleforge-api",),
        claims={"sf_uid": str(sf_uid), "acr": 0},
    )


def _bearer(sf_uid: UUID) -> dict[str, str]:
    return {"Authorization": f"Bearer {sf_uid}"}


def _register(client: TestClient, sf_uid: UUID) -> None:
    client.fake_validator.register(str(sf_uid), _principal(sf_uid))  # type: ignore[attr-defined]


async def _grant_org_admin(*, user_id: UUID, org_id: UUID, suspended: bool = False) -> None:
    """Writes directly into authz-svc's own tables (same Postgres, same
    schema it owns) -- mirrors authz-svc's own test helper, just via raw
    SQL since orgs-svc's tests can't import authz-svc's app code."""
    engine = create_async_engine(_AUTHZ_WRITE_DSN)
    status = "suspended" if suspended else "active"
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO role_assignments "
                "(id, user_id, role_id, domain, org_id, membership_id, status, "
                " granted_by, granted_at, version) "
                "VALUES (:id, :user_id, 'org_admin', :domain, :org_id, NULL, :status, "
                " :user_id, now(), 1)"
            ),
            {
                "id": uuid7(),
                "user_id": user_id,
                "domain": f"org:{org_id}",
                "org_id": org_id,
                "status": status,
            },
        )
        if not suspended:
            await conn.execute(
                text(
                    "INSERT INTO casbin_rule (ptype, v0, v1, v2) "
                    "VALUES ('g', :sub, 'role:org_admin', :dom)"
                ),
                {"sub": f"user:{user_id}", "dom": f"org:{org_id}"},
            )
        await conn.execute(text("UPDATE policy_version SET version = version + 1 WHERE id = 1"))
    await engine.dispose()


async def _reload_enforcer(app) -> None:  # type: ignore[no-untyped-def]
    await app.state.authz_enforcer.reload()


async def _create_real_org_with_member(*, org_id: UUID, member_id: UUID) -> None:
    """A real organizations + memberships row (not via the HTTP API --
    that path has its own full coverage in test_routes.py), so the
    positive-control case in this suite proves a true 200, not an
    accidental non-403 from some unrelated 404."""
    engine = create_async_engine(_ORGS_WRITE_DSN)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO organizations "
                "(id, slug, name, status, currency, shipping_flat_minor, tax_rate_bps, "
                " settings, created_by, created_at, updated_at, version) "
                "VALUES (:id, :slug, 'Tenant Isolation Test Org', 'active', 'usd', 0, 0, "
                " '{}', :member_id, now(), now(), 1)"
            ),
            {"id": org_id, "slug": f"tenant-isolation-{org_id.hex[:12]}", "member_id": member_id},
        )
        await conn.execute(
            text(
                "INSERT INTO memberships "
                "(id, org_id, user_id, status, invited_by, joined_at, created_at, "
                " updated_at, version) "
                "VALUES (:id, :org_id, :user_id, 'active', :user_id, now(), now(), now(), 1)"
            ),
            {"id": uuid7(), "org_id": org_id, "user_id": member_id},
        )
    await engine.dispose()


@pytest.fixture
def org_a_id() -> UUID:
    return uuid4()


@pytest.fixture
def org_b_id() -> UUID:
    return uuid4()


# --- GET /v1/orgs/{org_id}/members (read) -----------------------------------


def test_read_members_allowed_for_a_real_org_admin_on_their_own_org(
    client: TestClient, org_a_id: UUID
) -> None:
    """Positive control -- a real org, a real active membership, a real
    org_admin grant: must be a genuine 200."""
    actor = uuid7()
    asyncio.run(_create_real_org_with_member(org_id=org_a_id, member_id=actor))
    asyncio.run(_grant_org_admin(user_id=actor, org_id=org_a_id))
    asyncio.run(_reload_enforcer(client.app))
    _register(client, actor)

    resp = client.get(f"/v1/orgs/{org_a_id}/members", headers=_bearer(actor))

    assert resp.status_code == 200, resp.text


def test_read_members_denied_with_no_membership_at_all(client: TestClient, org_a_id: UUID) -> None:
    actor = uuid7()
    asyncio.run(_create_real_org_with_member(org_id=org_a_id, member_id=uuid7()))
    _register(client, actor)

    resp = client.get(f"/v1/orgs/{org_a_id}/members", headers=_bearer(actor))

    # Authorize itself never emits 404 -- only (401, 403). Accepting 404
    # here would mask a real bug: Authorize wrongly allowing the request,
    # with the handler's own downstream membership check 404ing instead.
    assert resp.status_code in (401, 403), resp.text


def test_read_members_denied_for_a_cross_org_admin(
    client: TestClient, org_a_id: UUID, org_b_id: UUID
) -> None:
    """org_admin in B must not read A, even though A is a real org that
    genuinely exists (ruling out "it 404'd because A doesn't exist")."""
    actor = uuid7()
    asyncio.run(_create_real_org_with_member(org_id=org_a_id, member_id=uuid7()))
    asyncio.run(_grant_org_admin(user_id=actor, org_id=org_b_id))
    asyncio.run(_reload_enforcer(client.app))
    _register(client, actor)

    resp = client.get(f"/v1/orgs/{org_a_id}/members", headers=_bearer(actor))

    assert resp.status_code in (401, 403), resp.text


def test_read_members_denied_for_a_suspended_grant(client: TestClient, org_a_id: UUID) -> None:
    """Simulates the post-membership.disabled state (role_assignment
    suspended, casbin g-row removed -- E2.8's own event_handlers.py tests
    cover the transition itself; this proves enforcement denies the
    resulting state), against a real org so a 404 can't be "it doesn't
    exist" instead of "access denied"."""
    actor = uuid7()
    asyncio.run(_create_real_org_with_member(org_id=org_a_id, member_id=actor))
    asyncio.run(_grant_org_admin(user_id=actor, org_id=org_a_id, suspended=True))
    asyncio.run(_reload_enforcer(client.app))
    _register(client, actor)

    resp = client.get(f"/v1/orgs/{org_a_id}/members", headers=_bearer(actor))

    assert resp.status_code in (401, 403), resp.text


# --- POST /v1/orgs/{org_id}/invitations (mutate) ----------------------------


def test_create_invitation_allowed_for_a_real_org_admin_on_their_own_org(
    client: TestClient, org_a_id: UUID
) -> None:
    """Positive control -- a real org, a real active membership, a real
    org_admin grant: must be a genuine 200, proving the matrix isn't
    vacuously rejecting everything regardless of Authorize's decision."""
    actor = uuid7()
    asyncio.run(_create_real_org_with_member(org_id=org_a_id, member_id=actor))
    asyncio.run(_grant_org_admin(user_id=actor, org_id=org_a_id))
    asyncio.run(_reload_enforcer(client.app))
    _register(client, actor)

    resp = client.post(
        f"/v1/orgs/{org_a_id}/invitations",
        headers=_bearer(actor),
        json={"email": "new-member@example.com", "roles_requested": ["org_admin"]},
    )

    assert resp.status_code == 200, resp.text


def test_create_invitation_denied_with_no_membership_at_all(
    client: TestClient, org_a_id: UUID
) -> None:
    actor = uuid7()
    asyncio.run(_create_real_org_with_member(org_id=org_a_id, member_id=uuid7()))
    _register(client, actor)

    resp = client.post(
        f"/v1/orgs/{org_a_id}/invitations",
        headers=_bearer(actor),
        json={"email": "attacker-invited@example.com", "roles_requested": ["org_admin"]},
    )

    assert resp.status_code in (401, 403), resp.text


def test_create_invitation_denied_for_a_cross_org_admin(
    client: TestClient, org_a_id: UUID, org_b_id: UUID
) -> None:
    actor = uuid7()
    asyncio.run(_create_real_org_with_member(org_id=org_a_id, member_id=uuid7()))
    asyncio.run(_grant_org_admin(user_id=actor, org_id=org_b_id))
    asyncio.run(_reload_enforcer(client.app))
    _register(client, actor)

    resp = client.post(
        f"/v1/orgs/{org_a_id}/invitations",
        headers=_bearer(actor),
        json={"email": "cross-tenant-invite@example.com", "roles_requested": ["org_admin"]},
    )

    assert resp.status_code in (401, 403), resp.text


def test_create_invitation_denied_for_a_suspended_grant(client: TestClient, org_a_id: UUID) -> None:
    actor = uuid7()
    asyncio.run(_create_real_org_with_member(org_id=org_a_id, member_id=actor))
    asyncio.run(_grant_org_admin(user_id=actor, org_id=org_a_id, suspended=True))
    asyncio.run(_reload_enforcer(client.app))
    _register(client, actor)

    resp = client.post(
        f"/v1/orgs/{org_a_id}/invitations",
        headers=_bearer(actor),
        json={"email": "suspended-admin-invite@example.com", "roles_requested": ["org_admin"]},
    )

    assert resp.status_code in (401, 403), resp.text
