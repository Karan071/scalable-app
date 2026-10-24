"""Client for catalog-svc's internal SKU lookup (LLD-06 §2's add-to-cart
validation).

Same client-credentials-token-caching shape as authz-svc's ``orgs_client.py``
-- duplicated rather than shared for now (already flagged there as a
follow-up cleanup to promote into ``sf_common``).
"""

import time
from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class CatalogSku:
    sku_id: str
    product_id: str
    product_name: str
    sku_status: str
    product_status: str
    price_minor: int

    @property
    def available(self) -> bool:
        return self.sku_status == "active" and self.product_status == "active"


class ServiceTokenSource:
    """Caches a client-credentials token for ``sf-svc-cart`` until shortly before expiry."""

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

    async def get_sku(self, org_id: str, sku_id: str) -> CatalogSku | None:
        token = await self._tokens.get()
        resp = await self._http.get(
            f"{self._base_url}/internal/v1/orgs/{org_id}/skus/{sku_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        body = resp.json()
        return CatalogSku(
            sku_id=body["sku_id"],
            product_id=body["product_id"],
            product_name=body["product_name"],
            sku_status=body["sku_status"],
            product_status=body["product_status"],
            price_minor=body["price_minor"],
        )
