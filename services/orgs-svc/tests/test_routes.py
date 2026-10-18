"""Integration tests against the local Compose Postgres (`make dev`). Skip if unreachable."""

from collections.abc import Iterator
from uuid import UUID, uuid4, uuid7

import pytest
from fastapi.testclient import TestClient
from sf_authz import Principal
from sf_common import TokenCipher
from sqlalchemy import text


@pytest.fixture
def client() -> Iterator[TestClient]:
    try:
        from app.main import app
    except Exception as exc:
        pytest.skip(f"could not import app.main: {exc}")

    from app.routes import require_internal_read_scope

    def fake_internal(scope: str) -> Principal:
        return Principal(
            subject="svc:authz", audiences=("scaleforge-internal",), claims={"scope": scope}
        )

    app.dependency_overrides[require_internal_read_scope] = lambda: fake_internal("orgs:read")

    try:
        with TestClient(app) as c:
            resp = c.get("/readyz")
            if resp.status_code != 200:
                pytest.skip(f"orgs-svc not ready (DB unreachable?): {resp.json()}")
            yield c
    except Exception as exc:
        pytest.skip(f"local Postgres not reachable: {exc}")
    finally:
        app.dependency_overrides.clear()


def _as_user(
    sf_uid: UUID, *, email: str = "owner@example.com", email_verified: bool = True
) -> None:
    """Overrides every auth dependency these tests exercise — including every
    `Authorize(...)` alias (E2.7/E2.8) — so tests keep bypassing the
    HTTP-layer gate entirely, same as before E2.7 (that gate's own logic is
    covered by sf-authz's dedicated tests, not re-tested per-service). The
    real event-driven grant this would otherwise require (membership.created
    -> authz-svc's consumer -> org_admin) is proven separately and for real
    in authz-svc's own test_e2e_membership_created.py — it can't be
    exercised here since authz-svc's consumer isn't a running process during
    orgs-svc's own tests."""
    from app import routes
    from app.main import app

    principal = Principal(
        subject=f"user:{sf_uid}",
        audiences=("scaleforge-api",),
        claims={"sf_uid": str(sf_uid), "email": email, "email_verified": email_verified},
    )
    for name in (
        "require_authenticated_user",
        "require_create_platform_org",
        "require_read_platform_org",
        "require_suspend_platform_org",
        "require_reactivate_platform_org",
        "require_archive_platform_org",
        "require_read_org_settings",
        "require_update_org_settings",
        "require_read_member",
        "require_disable_member",
        "require_reactivate_member",
        "require_delete_member",
        "require_read_invitation",
        "require_create_invitation",
        "require_update_invitation",
        "require_delete_invitation",
    ):
        app.dependency_overrides[getattr(routes, name)] = lambda principal=principal: principal


def _unique_slug() -> str:
    return f"store-{uuid7().hex[:12]}"


async def _decrypt_latest_invitation_token(org_id: str) -> tuple[str, str]:
    """Returns ``(invitation_id, raw_token)`` for the most recent invitation
    on this org — the only way a test can see the token, which by design
    never comes back from the API (it travels to the invitee by email)."""
    from app.db import build_orgs_engine, build_orgs_session_factory
    from app.settings import Settings

    settings = Settings()
    engine = build_orgs_engine(settings)
    factory = build_orgs_session_factory(engine)
    cipher = TokenCipher.from_base64_keys(
        settings.invitation_enc_keys, settings.invitation_enc_primary_kid
    )
    async with factory() as session:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT payload FROM outbox "
                        "WHERE payload->>'type' = 'invitation.created' "
                        "AND payload->'data'->>'org_id' = :org_id "
                        "ORDER BY occurred_at DESC LIMIT 1"
                    ),
                    {"org_id": org_id},
                )
            )
            .mappings()
            .one()
        )
    await engine.dispose()
    data = row["payload"]["data"]
    invitation_id = data["invitation_id"]
    raw_token = cipher.decrypt(data["token_enc"], aad=invitation_id.encode())
    return invitation_id, raw_token


def _create_org(
    client: TestClient, creator: UUID, *, owner_email: str = "owner@example.com"
) -> dict:
    """Creates an org and immediately accepts its owner invitation, so the
    returned org is ``active`` with ``creator`` as its first (org_admin)
    member — matching LLD-03 §4 (a fresh org has no members otherwise)."""
    _as_user(creator)
    resp = client.post(
        "/v1/platform/orgs",
        json={
            "slug": _unique_slug(),
            "name": "Test Store",
            "currency": "usd",
            "owner_email": owner_email,
        },
    )
    assert resp.status_code == 200, resp.text
    org = resp.json()

    import asyncio

    _invitation_id, raw_token = asyncio.run(_decrypt_latest_invitation_token(org["id"]))
    _as_user(creator, email=owner_email, email_verified=True)
    accept = client.post("/v1/invitations:accept", json={"token": raw_token})
    assert accept.status_code == 200, accept.text

    return client.get(f"/v1/orgs/{org['id']}").json()


def test_create_organization_starts_pending_until_owner_accepts(client: TestClient) -> None:
    _as_user(uuid4())
    resp = client.post(
        "/v1/platform/orgs",
        json={
            "slug": _unique_slug(),
            "name": "Test Store",
            "currency": "usd",
            "owner_email": "owner@example.com",
        },
    )
    assert resp.status_code == 200, resp.text
    org = resp.json()
    assert org["status"] == "pending"
    assert org["version"] == 1


def test_create_org_helper_bootstraps_an_active_org_with_owner_as_member(
    client: TestClient,
) -> None:
    creator = uuid4()
    org = _create_org(client, creator)

    assert org["status"] == "active"
    members = client.get(f"/v1/orgs/{org['id']}/members").json()
    assert any(m["user_id"] == str(creator) and m["status"] == "active" for m in members)


def test_non_member_cannot_read_organization(client: TestClient) -> None:
    org = _create_org(client, uuid4())
    _as_user(uuid4())  # a different, unrelated user

    resp = client.get(f"/v1/orgs/{org['id']}")

    assert resp.status_code == 404


def test_suspend_reactivate_archive_lifecycle(client: TestClient) -> None:
    creator = uuid4()
    org = _create_org(client, creator)

    suspend = client.post(f"/v1/platform/orgs/{org['id']}:suspend", json={"reason": "fraud review"})
    assert suspend.status_code == 200
    assert suspend.json()["status"] == "suspended"

    reactivate = client.post(f"/v1/platform/orgs/{org['id']}:reactivate", json={})
    assert reactivate.status_code == 200
    assert reactivate.json()["status"] == "active"

    archive = client.post(f"/v1/platform/orgs/{org['id']}:archive", json={"reason": "closed"})
    assert archive.status_code == 200
    assert archive.json()["status"] == "archived"


def test_invalid_transition_is_rejected(client: TestClient) -> None:
    _as_user(uuid4())
    created = client.post(
        "/v1/platform/orgs",
        json={
            "slug": _unique_slug(),
            "name": "Test Store",
            "currency": "usd",
            "owner_email": "owner@example.com",
        },
    )
    org = created.json()
    assert org["status"] == "pending"

    # reactivate only applies from "suspended"
    resp = client.post(f"/v1/platform/orgs/{org['id']}:reactivate", json={})

    assert resp.status_code == 409


def test_invitation_accept_creates_membership(client: TestClient) -> None:
    import asyncio

    creator = uuid4()
    org = _create_org(client, creator)
    _as_user(creator)

    created = client.post(
        f"/v1/orgs/{org['id']}/invitations",
        json={"email": "invitee@example.com", "roles_requested": ["org_admin"]},
    )
    assert created.status_code == 200, created.text

    _, raw_token = asyncio.run(_decrypt_latest_invitation_token(org["id"]))

    invitee = uuid4()
    _as_user(invitee, email="invitee@example.com", email_verified=True)
    accepted = client.post("/v1/invitations:accept", json={"token": raw_token})

    assert accepted.status_code == 200
    assert accepted.json()["org_id"] == org["id"]

    members = client.get(f"/v1/orgs/{org['id']}/members")
    assert members.status_code == 200
    assert any(m["user_id"] == str(invitee) and m["status"] == "active" for m in members.json())


def test_invitation_accept_rejects_email_mismatch(client: TestClient) -> None:
    import asyncio

    creator = uuid4()
    org = _create_org(client, creator)
    _as_user(creator)

    client.post(
        f"/v1/orgs/{org['id']}/invitations",
        json={"email": "invitee@example.com", "roles_requested": ["org_admin"]},
    )
    _, raw_token = asyncio.run(_decrypt_latest_invitation_token(org["id"]))

    _as_user(uuid4(), email="someone-else@example.com", email_verified=True)
    resp = client.post("/v1/invitations:accept", json={"token": raw_token})

    assert resp.status_code == 403


def test_revoked_invitation_cannot_be_accepted(client: TestClient) -> None:
    import asyncio

    creator = uuid4()
    org = _create_org(client, creator)
    _as_user(creator)

    client.post(
        f"/v1/orgs/{org['id']}/invitations",
        json={"email": "invitee@example.com", "roles_requested": ["org_admin"]},
    )
    invitation_id, raw_token = asyncio.run(_decrypt_latest_invitation_token(org["id"]))

    revoke = client.delete(f"/v1/orgs/{org['id']}/invitations/{invitation_id}")
    assert revoke.status_code == 200

    _as_user(uuid4(), email="invitee@example.com", email_verified=True)
    resp = client.post("/v1/invitations:accept", json={"token": raw_token})

    assert resp.status_code == 410


def test_unknown_invitation_token_gives_the_same_response_as_revoked(client: TestClient) -> None:
    _as_user(uuid4())
    resp = client.post("/v1/invitations:accept", json={"token": "not-a-real-token"})

    assert resp.status_code == 410


def test_member_disable_reactivate_and_revoke(client: TestClient) -> None:
    import asyncio

    creator = uuid4()
    org = _create_org(client, creator)
    _as_user(creator)
    client.post(
        f"/v1/orgs/{org['id']}/invitations",
        json={"email": "member@example.com", "roles_requested": ["catalog_manager"]},
    )
    _, raw_token = asyncio.run(_decrypt_latest_invitation_token(org["id"]))

    member = uuid4()
    _as_user(member, email="member@example.com", email_verified=True)
    client.post("/v1/invitations:accept", json={"token": raw_token})

    members = client.get(f"/v1/orgs/{org['id']}/members")
    membership_id = next(m["id"] for m in members.json() if m["user_id"] == str(member))

    _as_user(creator)
    disable = client.post(f"/v1/orgs/{org['id']}/members/{membership_id}:disable", json={})
    assert disable.status_code == 200

    reactivate = client.post(f"/v1/orgs/{org['id']}/members/{membership_id}:reactivate")
    assert reactivate.status_code == 200

    revoke = client.delete(f"/v1/orgs/{org['id']}/members/{membership_id}")
    assert revoke.status_code == 200


def test_leave_organization(client: TestClient) -> None:
    creator = uuid4()
    org = _create_org(client, creator)
    _as_user(creator)

    resp = client.post(f"/v1/orgs/{org['id']}/members/me:leave")
    assert resp.status_code == 200

    # revoked membership is no longer "active", so the tenant-isolation
    # check now denies the former member
    assert client.get(f"/v1/orgs/{org['id']}").status_code == 404


def test_leave_organization_without_membership_is_not_found(client: TestClient) -> None:
    org = _create_org(client, uuid4())
    _as_user(uuid4())  # never invited

    resp = client.post(f"/v1/orgs/{org['id']}/members/me:leave")
    assert resp.status_code == 404


def test_internal_membership_lookup(client: TestClient) -> None:
    import asyncio

    creator = uuid4()
    org = _create_org(client, creator)
    _as_user(creator)
    client.post(
        f"/v1/orgs/{org['id']}/invitations",
        json={"email": "internal@example.com", "roles_requested": ["org_admin"]},
    )
    _, raw_token = asyncio.run(_decrypt_latest_invitation_token(org["id"]))

    member = uuid4()
    _as_user(member, email="internal@example.com", email_verified=True)
    client.post("/v1/invitations:accept", json={"token": raw_token})

    members = client.get(f"/v1/orgs/{org['id']}/members")
    membership_id = next(m["id"] for m in members.json() if m["user_id"] == str(member))

    resp = client.get(f"/internal/v1/memberships/{membership_id}", params={"org_id": org["id"]})
    assert resp.status_code == 200
    assert resp.json()["status"] == "active"
