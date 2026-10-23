"""``KeycloakAdminClient.add_user_to_group`` against a mocked transport --
no real Keycloak involved, just the group-id lookup/cache/join sequence."""

from collections.abc import Callable

import httpx
import pytest
from app.keycloak_admin import GroupNotFoundError, KeycloakAdminClient


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> KeycloakAdminClient:
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return KeycloakAdminClient(
        http_client,
        admin_base_url="https://kc.test/admin/realms/scaleforge",
        token_endpoint="https://kc.test/realms/scaleforge/protocol/openid-connect/token",
        client_id="sf-users-admin",
        client_secret="secret",
    )


def _token_response() -> httpx.Response:
    return httpx.Response(200, json={"access_token": "tok", "expires_in": 300})


async def test_add_user_to_group_resolves_group_id_then_joins() -> None:
    put_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return _token_response()
        if request.url.path.endswith("/groups"):
            assert request.url.params["search"] == "mfa-required"
            return httpx.Response(200, json=[{"id": "group-123", "name": "mfa-required"}])
        if request.method == "PUT":
            put_calls.append(request.url.path)
            return httpx.Response(204)
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    client = _client(handler)
    await client.add_user_to_group(keycloak_user_id="kc-user-1", group_name="mfa-required")

    assert put_calls == ["/admin/realms/scaleforge/users/kc-user-1/groups/group-123"]


async def test_add_user_to_group_caches_the_group_id_across_calls() -> None:
    group_lookups = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal group_lookups
        if request.url.path.endswith("/token"):
            return _token_response()
        if request.url.path.endswith("/groups"):
            group_lookups += 1
            return httpx.Response(200, json=[{"id": "group-123", "name": "mfa-required"}])
        return httpx.Response(204)

    client = _client(handler)
    await client.add_user_to_group(keycloak_user_id="kc-user-1", group_name="mfa-required")
    await client.add_user_to_group(keycloak_user_id="kc-user-2", group_name="mfa-required")

    assert group_lookups == 1


async def test_add_user_to_group_raises_for_an_unknown_group() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            return _token_response()
        if request.url.path.endswith("/groups"):
            return httpx.Response(200, json=[])
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    client = _client(handler)
    with pytest.raises(GroupNotFoundError):
        await client.add_user_to_group(keycloak_user_id="kc-user-1", group_name="does-not-exist")
