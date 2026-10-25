"""mock-payment-provider settings (SF_* env vars).

No DB, no Keycloak: ADR-0008 places this service outside the trust
boundary, modelled on a real PSP's public API shape. ``webhook_secret``
simulates the shared secret a PSP and merchant provision out of band.
"""

from sf_common import BaseServiceSettings


class Settings(BaseServiceSettings):
    webhook_target_url: str = "http://localhost:8109/webhooks/payments/mock"
    webhook_secret: str
    #: Scales every scenario delay (LLD-06 §8.1's 1s/10s/15s/60s/120s).
    #: 1.0 for live/manual verification; tests use the `/__mock/...`
    #: control API instead of waiting, so this rarely needs changing, but
    #: it's here rather than hardcoding the real numbers everywhere.
    delay_scale: float = 1.0
