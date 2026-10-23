"""Keycloak Admin REST API client: ``sf_uid`` (LLD-01 §11.2) and the
enabled flag that follows the user's status (§11.3).

``sf-bff``'s ``sf_uid`` protocol mapper reads this user attribute. Without
it, every access token this user ever gets is permanently missing the claim
ADR-0009 says services rely on — confirmed live: a provisioned user's first
``/v1/me`` call 403'd with "token has no sf_uid claim yet" because nothing
had ever set the attribute (2026-10-18).
"""

import time

import httpx

# User fields a profile update must carry back so Keycloak doesn't clear them.
_PROFILE_FIELDS = ("username", "email", "emailVerified", "firstName", "lastName", "enabled")


class KeycloakUserNotFound(Exception):
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
        """Idempotent."""
        user = await self._get_user(keycloak_user_id)
        if user is None:
            raise KeycloakUserNotFound(keycloak_user_id)
        attributes = user.get("attributes") or {}
        attributes["sf_uid"] = [sf_uid]
        await self._put_user(user, attributes=attributes)

    async def sync_enabled(self, *, keycloak_user_id: str, enabled: bool) -> bool:
        """LLD-01 §11.3: enables or disables the Keycloak user; disabling
        also ends their Keycloak sessions, so SSO can't sign them back in.
        Idempotent. Returns False if Keycloak has no such user (nothing left
        to sign in with, so nothing to do)."""
        user = await self._get_user(keycloak_user_id)
        if user is None:
            return False
        if user.get("enabled") != enabled:
            await self._put_user(user, enabled=enabled)
        if not enabled:
            headers = await self._headers()
            resp = await self._http.post(
                f"{self._user_url(keycloak_user_id)}/logout", headers=headers
            )
            resp.raise_for_status()
        return True

    def _user_url(self, keycloak_user_id: str) -> str:
        return f"{self._admin_base_url}/users/{keycloak_user_id}"

    async def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {await self._get_token()}"}

    async def _get_user(self, keycloak_user_id: str) -> dict | None:
        resp = await self._http.get(self._user_url(keycloak_user_id), headers=await self._headers())
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]

    async def _put_user(self, user: dict, **changes: object) -> None:
        """Sends the user's whole profile back with ``changes`` applied:
        under Keycloak 26's declarative user profile, email and names are
        profile attributes too, and a PUT that omits them clears them.
        (Sending only ``attributes`` wiped every user's profile on first
        login: the next login was blocked on "Update profile", and tokens
        carried no email. See test_keycloak_admin_live.py.)"""
        profile = {key: user[key] for key in _PROFILE_FIELDS if key in user}
        if "attributes" in user:
            profile["attributes"] = user["attributes"]
        resp = await self._http.put(
            self._user_url(user["id"]), headers=await self._headers(), json={**profile, **changes}
        )
        resp.raise_for_status()
