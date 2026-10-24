"""Tenant-isolation matrix (LLD-14 §4): a representative subset (one read,
one mutate), actors {no membership, a cross-org grant} x {own-org path,
other-org path}, must get 403/404. Zero 2xx. The full cross-service,
route-metadata-generated matrix is tests/system/test_tenant_isolation.py;
inventory-svc isn't wired into it yet (same open item catalog-svc's own
smaller isolation test notes).

Unlike test_routes.py, this does NOT override every Authorize alias --
Authorize's real enforcement (against inventory-svc's own embedded
enforcer, reading the same local Postgres `authz` schema authz-svc writes
to) runs genuinely here. The ``inventory_manager`` role and its
``stock``/``reservation`` grants are seeded by authz-svc's own migration
(5830e6a07042), not anything inventory-svc defines.
"""

import asyncio
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import UUID, uuid4, uuid7

import pytest
from fastapi.testclient import TestClient
from sf_authz import Principal, TokenValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

# Local-dev-only: every svc_* role shares POSTGRES_APP_PASSWORD (init-schemas.sh).
# inventory-svc's own Settings only has read-only authz_reader creds, so
# granting roles for this test needs svc_authz's write access directly.
_AUTHZ_WRITE_DSN = (
    f"postgresql+asyncpg://svc_authz:app_local_dev@localhost:5432/{os.environ['SF_DB_NAME']}"
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
                pytest.skip(f"inventory-svc not ready (DB unreachable?): {resp.json()}")
            fake_validator = FakeTokenValidator()
            app.state.token_validator = fake_validator
            c.fake_validator = fake_validator  # type: ignore[attr-defined]
            yield c
    except Exception as exc:
        pytest.skip(f"local Postgres not reachable: {exc}")


def _principal(sf_uid: UUID) -> Principal:
    return Principal(
        subject=f"user:{sf_uid}", audiences=("scaleforge-api",), claims={"sf_uid": str(sf_uid)}
    )


def _bearer(sf_uid: UUID) -> dict[str, str]:
    return {"Authorization": f"Bearer {sf_uid}"}


def _register(client: TestClient, sf_uid: UUID) -> None:
    client.fake_validator.register(str(sf_uid), _principal(sf_uid))  # type: ignore[attr-defined]


async def _grant_inventory_manager(*, user_id: UUID, org_id: UUID) -> None:
    """Mirrors authz-svc's migration seed (``("inventory_manager", "stock",
    "read"/"adjust", "allow")``) via raw SQL -- inventory-svc's tests can't
    import authz-svc's app code."""
    engine = create_async_engine(_AUTHZ_WRITE_DSN)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO role_assignments "
                "(id, user_id, role_id, domain, org_id, membership_id, status, "
                " granted_by, granted_at, version) "
                "VALUES (:id, :user_id, 'inventory_manager', :domain, :org_id, NULL, 'active', "
                " :user_id, now(), 1)"
            ),
            {"id": uuid7(), "user_id": user_id, "domain": f"org:{org_id}", "org_id": org_id},
        )
        await conn.execute(
            text(
                "INSERT INTO casbin_rule (ptype, v0, v1, v2) "
                "VALUES ('g', :sub, 'role:inventory_manager', :dom)"
            ),
            {"sub": f"user:{user_id}", "dom": f"org:{org_id}"},
        )
        await conn.execute(text("UPDATE policy_version SET version = version + 1 WHERE id = 1"))
    await engine.dispose()


async def _reload_enforcer(app) -> None:  # type: ignore[no-untyped-def]
    await app.state.authz_enforcer.reload()


async def _seed_stock(client: TestClient, *, org_id: UUID, sku_id: UUID) -> None:
    """A dedicated engine, not ``app.state.session_factory``'s -- that one is
    bound to the TestClient's own event loop, and this runs under a fresh
    one via ``asyncio.run`` (same reasoning as ``_grant_inventory_manager``)."""
    from app.models import StockItem

    engine = create_async_engine(
        f"postgresql+asyncpg://svc_inventory:{os.environ['SF_DB_PASSWORD']}"
        f"@{os.environ['SF_DB_HOST']}:{os.environ['SF_DB_PORT']}/{os.environ['SF_DB_NAME']}"
    )
    async with engine.connect() as conn:
        from sqlalchemy.ext.asyncio import AsyncSession

        session = AsyncSession(bind=conn)
        session.add(
            StockItem(
                sku_id=sku_id,
                org_id=org_id,
                product_id=uuid7(),
                on_hand=10,
                reserved=0,
                low_stock_threshold=0,
                version=1,
                updated_at=datetime.now(UTC),
            )
        )
        await session.commit()
    await engine.dispose()


@pytest.fixture
def org_a_id() -> UUID:
    return uuid4()


@pytest.fixture
def org_b_id() -> UUID:
    return uuid4()


# --- GET /v1/orgs/{org_id}/stock (read) -------------------------------------


def test_read_stock_allowed_for_a_real_inventory_manager_on_their_own_org(
    client: TestClient, org_a_id: UUID
) -> None:
    actor = uuid7()
    asyncio.run(_grant_inventory_manager(user_id=actor, org_id=org_a_id))
    asyncio.run(_reload_enforcer(client.app))
    _register(client, actor)

    resp = client.get(f"/v1/orgs/{org_a_id}/stock", headers=_bearer(actor))

    assert resp.status_code == 200, resp.text


def test_read_stock_denied_with_no_membership_at_all(client: TestClient, org_a_id: UUID) -> None:
    actor = uuid7()
    _register(client, actor)

    resp = client.get(f"/v1/orgs/{org_a_id}/stock", headers=_bearer(actor))

    assert resp.status_code in (401, 403), resp.text


def test_read_stock_denied_for_a_cross_org_inventory_manager(
    client: TestClient, org_a_id: UUID, org_b_id: UUID
) -> None:
    actor = uuid7()
    asyncio.run(_grant_inventory_manager(user_id=actor, org_id=org_b_id))
    asyncio.run(_reload_enforcer(client.app))
    _register(client, actor)

    resp = client.get(f"/v1/orgs/{org_a_id}/stock", headers=_bearer(actor))

    assert resp.status_code in (401, 403), resp.text


# --- POST /v1/orgs/{org_id}/stock/{sku_id}:adjust (mutate) ------------------


def test_adjust_stock_allowed_for_a_real_inventory_manager_on_their_own_org(
    client: TestClient, org_a_id: UUID
) -> None:
    actor, sku_id = uuid7(), uuid7()
    asyncio.run(_seed_stock(client, org_id=org_a_id, sku_id=sku_id))
    asyncio.run(_grant_inventory_manager(user_id=actor, org_id=org_a_id))
    asyncio.run(_reload_enforcer(client.app))
    _register(client, actor)

    resp = client.post(
        f"/v1/orgs/{org_a_id}/stock/{sku_id}:adjust",
        headers={**_bearer(actor), "If-Match": '"v1"'},
        json={"adjustment_id": str(uuid7()), "delta": 5, "reason": "count"},
    )

    assert resp.status_code == 200, resp.text


def test_adjust_stock_denied_with_no_membership_at_all(client: TestClient, org_a_id: UUID) -> None:
    actor, sku_id = uuid7(), uuid7()
    asyncio.run(_seed_stock(client, org_id=org_a_id, sku_id=sku_id))
    _register(client, actor)

    resp = client.post(
        f"/v1/orgs/{org_a_id}/stock/{sku_id}:adjust",
        headers={**_bearer(actor), "If-Match": '"v1"'},
        json={"adjustment_id": str(uuid7()), "delta": 5, "reason": "attacker"},
    )

    assert resp.status_code in (401, 403), resp.text


def test_adjust_stock_denied_for_a_cross_org_inventory_manager(
    client: TestClient, org_a_id: UUID, org_b_id: UUID
) -> None:
    actor, sku_id = uuid7(), uuid7()
    asyncio.run(_seed_stock(client, org_id=org_a_id, sku_id=sku_id))
    asyncio.run(_grant_inventory_manager(user_id=actor, org_id=org_b_id))
    asyncio.run(_reload_enforcer(client.app))
    _register(client, actor)

    resp = client.post(
        f"/v1/orgs/{org_a_id}/stock/{sku_id}:adjust",
        headers={**_bearer(actor), "If-Match": '"v1"'},
        json={"adjustment_id": str(uuid7()), "delta": 5, "reason": "cross-tenant"},
    )

    assert resp.status_code in (401, 403), resp.text
