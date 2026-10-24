"""Client for catalog-svc's internal export endpoint (LLD-05 §9 step 1).

Same client-credentials-token-caching shape as authz-svc's ``orgs_client.py``
-- duplicated rather than shared for now (already flagged there as a
follow-up cleanup to promote into ``sf_common``).
"""

import time

import httpx


class ServiceTokenSource:
    """Caches a client-credentials token for ``sf-svc-search`` until shortly before expiry."""

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


class CatalogClient:
    def __init__(
        self, http_client: httpx.AsyncClient, *, base_url: str, token_source: ServiceTokenSource
    ) -> None:
        self._http = http_client
        self._base_url = base_url.rstrip("/")
        self._tokens = token_source

    async def export_page(
        self, org_id: str, *, cursor: str | None, limit: int = 48
    ) -> tuple[list[dict], str | None]:
        token = await self._tokens.get()
        params: dict[str, str | int] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        resp = await self._http.get(
            f"{self._base_url}/internal/v1/orgs/{org_id}/products:export",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
        body = resp.json()
        return body["items"], body["next_cursor"]
