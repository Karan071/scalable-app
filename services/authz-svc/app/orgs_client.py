"""Client for orgs-svc's internal membership endpoint (LLD-02 §7 step 0, ADR-0009 §2).

Same client-credentials-token-caching shape as ``services/bff/app/users_client.py``'s
``ServiceTokenSource`` — duplicated rather than shared for now (flagged as a
follow-up cleanup to promote into ``sf_common``, not attempted in this slice).
"""

import time
from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class MembershipStatus:
    id: str
    org_id: str
    user_id: str
    status: str
    version: int


class ServiceTokenSource:
    """Caches a client-credentials token for ``sf-svc-authz`` until shortly before expiry."""

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


class OrgsClient:
    def __init__(
        self, http_client: httpx.AsyncClient, *, base_url: str, token_source: ServiceTokenSource
    ) -> None:
        self._http = http_client
        self._base_url = base_url.rstrip("/")
        self._tokens = token_source

    async def get_membership(self, membership_id: str, *, org_id: str) -> MembershipStatus | None:
        token = await self._tokens.get()
        resp = await self._http.get(
            f"{self._base_url}/internal/v1/memberships/{membership_id}",
            params={"org_id": org_id},
            headers={"Authorization": f"Bearer {token}"},
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        body = resp.json()
        return MembershipStatus(
            id=body["id"],
            org_id=body["org_id"],
            user_id=body["user_id"],
            status=body["status"],
            version=body["version"],
        )
