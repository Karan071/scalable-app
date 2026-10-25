"""The ``PaymentProvider`` port (ADR-0008): one client class today
(``MockProviderClient``, against mock-payment-provider), written so a real
sandbox PSP could be swapped in later without touching ``service.py``.
"""

from dataclasses import dataclass
from typing import Any

import httpx

#: ADR-0008: "5 s timeout" on every provider call.
_PROVIDER_TIMEOUT_S = 5.0


class ProviderDeclined(Exception):
    pass


class ProviderIntentNotFound(Exception):
    pass


class AuthorizationExpired(Exception):
    pass


class CaptureFailed(Exception):
    pass


@dataclass(frozen=True)
class ProviderIntent:
    id: str
    status: str
    sequence: int
    amount_minor: int
    currency: str


@dataclass(frozen=True)
class ProviderRefund:
    id: str
    payment_intent_id: str
    status: str


class MockProviderClient:
    """Talks to mock-payment-provider's PSP-shaped API (LLD-06 §8)."""

    def __init__(self, http_client: httpx.AsyncClient, *, base_url: str) -> None:
        self._http = http_client
        self._base_url = base_url.rstrip("/")

    def _timeout(self) -> httpx.Timeout:
        return httpx.Timeout(_PROVIDER_TIMEOUT_S, connect=2.0)

    async def authorize(
        self,
        *,
        idempotency_key: str,
        amount_minor: int,
        currency: str,
        payment_method_token: str,
        metadata: dict[str, str],
    ) -> ProviderIntent:
        resp = await self._http.post(
            f"{self._base_url}/v1/payment_intents",
            headers={"Idempotency-Key": idempotency_key},
            json={
                "amount_minor": amount_minor,
                "currency": currency,
                "payment_method_token": payment_method_token,
                "metadata": metadata,
            },
            timeout=self._timeout(),
        )
        if resp.status_code == 402:
            raise ProviderDeclined(resp.json())
        resp.raise_for_status()
        return _intent_from_body(resp.json())

    async def capture(self, *, idempotency_key: str, provider_intent_id: str) -> ProviderIntent:
        resp = await self._http.post(
            f"{self._base_url}/v1/payment_intents/{provider_intent_id}/capture",
            headers={"Idempotency-Key": idempotency_key},
            timeout=self._timeout(),
        )
        if resp.status_code == 404:
            raise ProviderIntentNotFound(provider_intent_id)
        if resp.status_code == 409:
            raise AuthorizationExpired(provider_intent_id)
        if resp.status_code >= 500:
            raise CaptureFailed(provider_intent_id)
        resp.raise_for_status()
        return _intent_from_body(resp.json())

    async def void(self, *, idempotency_key: str, provider_intent_id: str) -> ProviderIntent:
        resp = await self._http.post(
            f"{self._base_url}/v1/payment_intents/{provider_intent_id}/cancel",
            headers={"Idempotency-Key": idempotency_key},
            timeout=self._timeout(),
        )
        if resp.status_code == 404:
            raise ProviderIntentNotFound(provider_intent_id)
        resp.raise_for_status()
        return _intent_from_body(resp.json())

    async def refund(
        self, *, idempotency_key: str, provider_intent_id: str, amount_minor: int
    ) -> ProviderRefund:
        resp = await self._http.post(
            f"{self._base_url}/v1/payment_intents/{provider_intent_id}/refunds",
            headers={"Idempotency-Key": idempotency_key},
            json={"amount_minor": amount_minor},
            timeout=self._timeout(),
        )
        if resp.status_code == 404:
            raise ProviderIntentNotFound(provider_intent_id)
        resp.raise_for_status()
        body = resp.json()
        return ProviderRefund(
            id=body["id"], payment_intent_id=body["payment_intent"], status=body["status"]
        )

    async def get(self, provider_intent_id: str) -> ProviderIntent:
        resp = await self._http.get(
            f"{self._base_url}/v1/payment_intents/{provider_intent_id}", timeout=self._timeout()
        )
        if resp.status_code == 404:
            raise ProviderIntentNotFound(provider_intent_id)
        resp.raise_for_status()
        return _intent_from_body(resp.json())


def _intent_from_body(body: dict[str, Any]) -> ProviderIntent:
    return ProviderIntent(
        id=body["id"],
        status=body["status"],
        sequence=body["sequence"],
        amount_minor=body["amount_minor"],
        currency=body["currency"],
    )
