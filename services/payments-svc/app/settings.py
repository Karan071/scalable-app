"""payments-svc settings (SF_* env vars, HLD §11.4: fail fast on invalid config).

No Casbin enforcer here (unlike every other service): every route is
either ``Internal(...)`` (orders-svc, the only caller) or the webhook
(``Public``, protected by its own HMAC signature check, not Casbin) --
there's no ``Authorize(...)`` route needing a live policy, so there's no
``authz_reader``/redis-watcher wiring to set up.
"""

from sf_common import BaseServiceSettings


class Settings(BaseServiceSettings):
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "scaleforge"
    db_user: str = "svc_payments"
    db_password: str

    oidc_issuer: str
    oidc_jwks_uri: str

    # Outbox relay + inbox consumers (LLD-08 §5-§6).
    rabbitmq_host: str = "localhost"
    rabbitmq_port: int = 5672
    rabbitmq_vhost: str = "/"
    rabbitmq_user: str
    rabbitmq_password: str

    # The mock PSP (ADR-0008): outside the trust boundary -- a plain base
    # URL, not a Keycloak client-credentials token. It's a disposable
    # sandbox with no auth of its own to configure here.
    provider_base_url: str = "http://localhost:8109"

    # Webhook verification (LLD-06 §8.2). A list so a rotation has both
    # the old and new secret active at once; comma-separated in the env.
    webhook_secrets: str
    webhook_clock_skew_tolerance_seconds: int = 300
    webhook_max_body_bytes: int = 64 * 1024

    reconciliation_interval_seconds: float = 30.0
    reconciliation_unknown_after_seconds: float = 30.0

    def webhook_secret_list(self) -> list[str]:
        return [s for s in self.webhook_secrets.split(",") if s]
