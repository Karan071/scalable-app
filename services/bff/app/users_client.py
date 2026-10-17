"""Client for users-svc's internal provisioning API (LLD-01 §4, ADR-0009 §2)."""

import time
from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class ProvisionResult:
    user_id: str
    status: str
    needs_claim_refresh: bool


class ServiceTokenSource:
    """Caches a client-credentials token for ``sf-svc-bff`` until shortly before expiry."""

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        *,
        token_endpoint: str,
        client_id: str,
        client_secret: str,
    ) -> None:
        self._http = http_client
        self._token_endpoint = token_endpoint
        self._client_id = client_id
        self._client_secret = client_secret
        self._token: str | None = None
        self._expires_at: float = 0.0

    async def get(self) -> str:
        if self._token is not None and time.monotonic() < self._expires_at - 10:
            return self._token
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
        self._token = body["access_token"]
        self._expires_at = time.monotonic() + body["expires_in"]
        return self._token


class UsersClient:
    def __init__(
        self, http_client: httpx.AsyncClient, *, base_url: str, token_source: ServiceTokenSource
    ) -> None:
        self._http = http_client
        self._base_url = base_url.rstrip("/")
        self._tokens = token_source

    async def provision(
        self, *, idp_issuer: str, idp_subject: str, email: str, email_verified: bool
    ) -> ProvisionResult:
        token = await self._tokens.get()
        resp = await self._http.post(
            f"{self._base_url}/internal/v1/users:provision",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "idp_issuer": idp_issuer,
                "idp_subject": idp_subject,
                "email": email,
                "email_verified": email_verified,
            },
        )
        resp.raise_for_status()
        body = resp.json()
        return ProvisionResult(
            user_id=body["user_id"],
            status=body["status"],
            needs_claim_refresh=body["needs_claim_refresh"],
        )

    async def get_me(self, access_token: str) -> dict[str, object]:
        resp = await self._http.get(
            f"{self._base_url}/v1/me", headers={"Authorization": f"Bearer {access_token}"}
        )
        resp.raise_for_status()
        result: dict[str, object] = resp.json()
        return result
