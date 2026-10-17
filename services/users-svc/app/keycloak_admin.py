"""Keycloak Admin REST API client for setting ``sf_uid`` (LLD-01 §11.2).

``sf-bff``'s ``sf_uid`` protocol mapper reads this user attribute. Without
it, every access token this user ever gets is permanently missing the claim
ADR-0009 says services rely on — confirmed live: a provisioned user's first
``/v1/me`` call 403'd with "token has no sf_uid claim yet" because nothing
had ever set the attribute (2026-10-18).
"""

import time

import httpx


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

    async def set_sf_uid(self, *, keycloak_user_id: str, sf_uid: str) -> None:
        """Idempotent. Keycloak's user-update PUT replaces the whole
        ``attributes`` map, not just the key sent, so this merges locally
        first rather than clobbering any other attribute."""
        token = await self._get_token()
        headers = {"Authorization": f"Bearer {token}"}
        url = f"{self._admin_base_url}/users/{keycloak_user_id}"

        get_resp = await self._http.get(url, headers=headers)
        get_resp.raise_for_status()
        user = get_resp.json()

        attributes = user.get("attributes") or {}
        attributes["sf_uid"] = [sf_uid]

        put_resp = await self._http.put(url, headers=headers, json={"attributes": attributes})
        put_resp.raise_for_status()
