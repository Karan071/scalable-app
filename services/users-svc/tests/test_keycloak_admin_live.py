"""``KeycloakAdminClient`` against the real Compose Keycloak (skips if it's
not reachable). Mocked-transport tests can't catch Keycloak's own update
semantics, which is where the bug this guards against lived."""

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio

_KC = "http://localhost:8081"
_REALM = f"{_KC}/admin/realms/scaleforge"


def _dotenv() -> dict[str, str]:
    path = Path(__file__).resolve().parents[3] / ".env"
    if not path.exists():
        return {}
    return dict(
        line.split("=", 1)
        for line in path.read_text().splitlines()
        if line and not line.startswith("#") and "=" in line
    )


_ENV = _dotenv()
_PASSWORD = f"Pw-{uuid4().hex}"


def _master_admin_token() -> str:
    resp = httpx.post(
        f"{_KC}/realms/master/protocol/openid-connect/token",
        data={
            "client_id": "admin-cli",
            "grant_type": "password",
            "username": "admin",
            "password": _ENV["KEYCLOAK_ADMIN_PASSWORD"],
        },
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


@pytest.fixture
def keycloak_user() -> Iterator[str]:
    """A throwaway user with a complete profile; deleted afterwards."""
    try:
        headers = {"authorization": f"Bearer {_master_admin_token()}"}
    except (httpx.HTTPError, KeyError) as exc:
        pytest.skip(f"local Keycloak not reachable: {exc}")
    username = f"kc-admin-test-{uuid4().hex[:10]}"
    resp = httpx.post(
        f"{_REALM}/users",
        headers=headers,
        json={
            "username": username,
            "email": f"{username}@example.com",
            "emailVerified": True,
            "firstName": "Ada",
            "lastName": "Lovelace",
            "enabled": True,
            "credentials": [{"type": "password", "value": _PASSWORD, "temporary": False}],
        },
    )
    resp.raise_for_status()
    user_id = resp.headers["location"].rsplit("/", 1)[-1]
    try:
        yield user_id
    finally:
        httpx.delete(f"{_REALM}/users/{user_id}", headers=headers)


@pytest_asyncio.fixture
async def admin_client() -> AsyncIterator[object]:
    from app.keycloak_admin import KeycloakAdminClient

    async with httpx.AsyncClient() as http:
        yield KeycloakAdminClient(
            http,
            admin_base_url=_REALM,
            token_endpoint=f"{_KC}/realms/scaleforge/protocol/openid-connect/token",
            client_id="sf-users-admin",
            client_secret=_ENV["SF_USERS_ADMIN_CLIENT_SECRET"],
        )


async def test_set_sf_uid_keeps_the_rest_of_the_profile(
    keycloak_user: str, admin_client: object
) -> None:
    """Keycloak 26's declarative user profile treats email and names as
    profile attributes: a PUT carrying only ``attributes`` clears them. That
    wiped every user's profile on first login, so the next login was blocked
    on "Update profile" and tokens carried no email."""
    sf_uid = str(uuid4())

    await admin_client.set_sf_uid(keycloak_user_id=keycloak_user, sf_uid=sf_uid)  # type: ignore[attr-defined]

    headers = {"authorization": f"Bearer {_master_admin_token()}"}
    user = httpx.get(f"{_REALM}/users/{keycloak_user}", headers=headers).json()
    assert user["attributes"]["sf_uid"] == [sf_uid]
    assert user.get("email", "").endswith("@example.com")
    assert user.get("firstName") == "Ada"
    assert user.get("lastName") == "Lovelace"
    assert user.get("emailVerified") is True


def _user_and_sessions(user_id: str) -> tuple[dict, list[dict]]:
    headers = {"authorization": f"Bearer {_master_admin_token()}"}
    user = httpx.get(f"{_REALM}/users/{user_id}", headers=headers).json()
    sessions = httpx.get(f"{_REALM}/users/{user_id}/sessions", headers=headers).json()
    return user, sessions


async def test_disabling_ends_keycloak_sessions_and_keeps_the_profile(
    keycloak_user: str, admin_client: object
) -> None:
    """LLD-01 §11.3: a disabled user's Keycloak SSO session must not outlive
    their BFF sessions, or the next login would sign them straight back in."""
    username = _user_and_sessions(keycloak_user)[0]["username"]
    # admin-cli (public, direct grants) is just a quick way to open a session.
    httpx.post(
        f"{_KC}/realms/scaleforge/protocol/openid-connect/token",
        data={
            "client_id": "admin-cli",
            "grant_type": "password",
            "username": username,
            "password": _PASSWORD,
        },
    ).raise_for_status()
    assert len(_user_and_sessions(keycloak_user)[1]) == 1

    found = await admin_client.sync_enabled(keycloak_user_id=keycloak_user, enabled=False)  # type: ignore[attr-defined]

    user, sessions = _user_and_sessions(keycloak_user)
    assert found is True
    assert user["enabled"] is False
    assert sessions == []
    assert user.get("email", "").endswith("@example.com")
    assert user.get("firstName") == "Ada"

    await admin_client.sync_enabled(keycloak_user_id=keycloak_user, enabled=True)  # type: ignore[attr-defined]

    assert _user_and_sessions(keycloak_user)[0]["enabled"] is True


async def test_status_sync_for_a_user_keycloak_no_longer_has(admin_client: object) -> None:
    try:
        _master_admin_token()
    except (httpx.HTTPError, KeyError) as exc:
        pytest.skip(f"local Keycloak not reachable: {exc}")

    found = await admin_client.sync_enabled(keycloak_user_id=str(uuid4()), enabled=False)  # type: ignore[attr-defined]

    assert found is False
