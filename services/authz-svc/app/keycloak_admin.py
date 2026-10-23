"""Keycloak Admin REST API client for mfa-required group membership
(LLD-01 §3.1, §9, accepted: "granting any platform role or org_admin adds
the user to the Keycloak group mfa-required (authz-svc -> Keycloak admin
API, from the outbox, idempotent)").

Deliberately separate from users-svc's own ``KeycloakAdminClient``: the two
use different, dedicated client-credentials clients (``sf-authz-admin`` vs
``sf-users-admin``) for the same least-privilege reason neither uses the
realm admin. This one never touches the ``sf_uid`` attribute, and
users-svc's never touches group membership.
"""

import time

import httpx


class GroupNotFoundError(Exception):
    pass


class KeycloakAdminClient:
    def __init__(
        self,
        http_client: httpx.AsyncClient,
        *,
        admin_base_url: str,
        token_endpoint: str,
        client_id: str,
        client_secret: str,
    ) -> None:
        self._http = http_client
        self._admin_base_url = admin_base_url.rstrip("/")
        self._token_endpoint = token_endpoint
        self._client_id = client_id
        self._client_secret = client_secret
        self._cached_token: str | None = None
        self._expires_at = 0.0
        self._group_id_cache: dict[str, str] = {}

    async def _get_token(self) -> str:
        if self._cached_token is not None and time.monotonic() < self._expires_at - 10:
            return self._cached_token
        resp = await self._http.post(
            self._token_endpoint,
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
        )
        resp.raise_for_status()
        body = resp.json()
        self._cached_token = body["access_token"]
        self._expires_at = time.monotonic() + body["expires_in"]
        return self._cached_token

    async def find_keycloak_id_by_sf_uid(self, sf_uid: str) -> str | None:
        """Resolves our internal ``user_id`` to Keycloak's own user id by
        searching on the ``sf_uid`` user attribute (set by users-svc at
        provisioning time, LLD-01 §11.2) -- this client never calls
        users-svc directly, only Keycloak, same as the rest of this class.
        """
        token = await self._get_token()
        headers = {"Authorization": f"Bearer {token}"}
        resp = await self._http.get(
            f"{self._admin_base_url}/users",
            headers=headers,
            params={"q": f"sf_uid:{sf_uid}"},
        )
        resp.raise_for_status()
        matches = resp.json()
        if not matches:
            return None
        return str(matches[0]["id"])

    async def add_user_to_group(self, *, keycloak_user_id: str, group_name: str) -> None:
        """Idempotent (LLD-08 §6): Keycloak's join-group PUT is a no-op if
        the user is already a member. There is no corresponding remove,
        since nothing emits this command on role revocation yet (a
        separate, open item -- see events.py's docstring)."""
        token = await self._get_token()
        headers = {"Authorization": f"Bearer {token}"}
        group_id = await self._get_group_id(group_name, headers=headers)
        url = f"{self._admin_base_url}/users/{keycloak_user_id}/groups/{group_id}"
        put_resp = await self._http.put(url, headers=headers)
        put_resp.raise_for_status()

    async def _get_group_id(self, group_name: str, *, headers: dict[str, str]) -> str:
        if group_name in self._group_id_cache:
            return self._group_id_cache[group_name]
        resp = await self._http.get(
            f"{self._admin_base_url}/groups",
            headers=headers,
            params={"search": group_name, "exact": "true"},
        )
        resp.raise_for_status()
        matches = [g for g in resp.json() if g["name"] == group_name]
        if not matches:
            raise GroupNotFoundError(group_name)
        group_id = matches[0]["id"]
        self._group_id_cache[group_name] = group_id
        return group_id
