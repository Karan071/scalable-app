"""A fake standing in for mock-payment-provider's HTTP API (not running
during payments-svc's own tests). Implements the same interface as
``provider_client.MockProviderClient`` -- same magic-token scenarios
(LLD-06 §8.1), so these tests read the same way an integration test
against the real mock provider would.
"""

from uuid import uuid4

import httpx
from app.provider_client import (
    CaptureFailed,
    ProviderDeclined,
    ProviderIntent,
    ProviderIntentNotFound,
    ProviderRefund,
)


class FakeProviderClient:
    def __init__(self) -> None:
        self.intents: dict[str, dict] = {}
        self.idempotency: dict[str, ProviderIntent] = {}
        self.always_fail_capture: set[str] = set()
        self.timeout_tokens: set[str] = set()

    async def authorize(
        self,
        *,
        idempotency_key: str,
        amount_minor: int,
        currency: str,
        payment_method_token: str,
        metadata: dict,
    ) -> ProviderIntent:
        if idempotency_key in self.idempotency:
            return self.idempotency[idempotency_key]
        if payment_method_token == "pm_mock_decline":
            raise ProviderDeclined({"error": {"code": "card_declined"}})
        if payment_method_token in self.timeout_tokens:
            raise httpx.ConnectError("connection refused (fake)")
        intent_id = str(uuid4())
        self.intents[intent_id] = {
            "status": "authorized",
            "sequence": 1,
            "amount_minor": amount_minor,
            "currency": currency,
        }
        result = ProviderIntent(
            id=intent_id,
            status="authorized",
            sequence=1,
            amount_minor=amount_minor,
            currency=currency,
        )
        self.idempotency[idempotency_key] = result
        return result

    async def capture(self, *, idempotency_key: str, provider_intent_id: str) -> ProviderIntent:
        if idempotency_key in self.idempotency:
            return self.idempotency[idempotency_key]
        intent = self.intents.get(provider_intent_id)
        if intent is None:
            raise ProviderIntentNotFound(provider_intent_id)
        if provider_intent_id in self.always_fail_capture:
            raise CaptureFailed(provider_intent_id)
        intent["status"] = "captured"
        intent["sequence"] += 1
        result = ProviderIntent(
            id=provider_intent_id,
            status="captured",
            sequence=intent["sequence"],
            amount_minor=intent["amount_minor"],
            currency=intent["currency"],
        )
        self.idempotency[idempotency_key] = result
        return result

    async def void(self, *, idempotency_key: str, provider_intent_id: str) -> ProviderIntent:
        intent = self.intents.get(provider_intent_id)
        if intent is None:
            raise ProviderIntentNotFound(provider_intent_id)
        intent["status"] = "voided"
        intent["sequence"] += 1
        return ProviderIntent(
            id=provider_intent_id,
            status="voided",
            sequence=intent["sequence"],
            amount_minor=intent["amount_minor"],
            currency=intent["currency"],
        )

    async def refund(
        self, *, idempotency_key: str, provider_intent_id: str, amount_minor: int
    ) -> ProviderRefund:
        intent = self.intents.get(provider_intent_id)
        if intent is None:
            raise ProviderIntentNotFound(provider_intent_id)
        return ProviderRefund(
            id=str(uuid4()), payment_intent_id=provider_intent_id, status="refunded"
        )

    async def get(self, provider_intent_id: str) -> ProviderIntent:
        intent = self.intents.get(provider_intent_id)
        if intent is None:
            raise ProviderIntentNotFound(provider_intent_id)
        return ProviderIntent(
            id=provider_intent_id,
            status=intent["status"],
            sequence=intent["sequence"],
            amount_minor=intent["amount_minor"],
            currency=intent["currency"],
        )

    def force_capture_fail(self, provider_intent_id: str) -> None:
        self.always_fail_capture.add(provider_intent_id)

    def force_authorize_timeout(self, token: str) -> None:
        self.timeout_tokens.add(token)
