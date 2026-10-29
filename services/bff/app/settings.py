"""bff settings (LLD-01 §13)."""

import json

from pydantic import field_validator
from sf_common import BaseServiceSettings


class Settings(BaseServiceSettings):
    oidc_issuer: str
    oidc_client_id: str
    oidc_client_secret: str
    oidc_redirect_uri: str
    oidc_jwks_uri: str | None = None  # discovered at startup if unset

    app_origin: str
    return_to_allowed_prefixes: list[str] = ["/"]
    # apps/web's built SPA (`pnpm build` -> dist/). Unset locally skips the
    # static/SPA-fallback route entirely (every existing deployment and the
    # system-test harness never set this, so behavior there is unchanged);
    # in Kubernetes this stays unset too (LLD-12 §6 routes static assets to
    # a separate `web` backend at the gateway) -- this is a local-dev-only
    # convenience so a real browser can complete a login round trip without
    # that gateway in front (LLD-15 §11's own "as built" note).
    web_dist_dir: str | None = None
    # Production is always True; local HTTP-only testing (no TLS terminator
    # in front of this process) sets it False so cookies actually round-trip.
    cookie_secure: bool = True

    svc_bff_client_id: str = "sf-svc-bff"
    svc_bff_client_secret: str
    users_svc_base_url: str
    orgs_svc_base_url: str
    authz_svc_base_url: str
    catalog_svc_base_url: str
    search_svc_base_url: str
    cart_svc_base_url: str
    inventory_svc_base_url: str
    orders_svc_base_url: str
    # payments-svc has no entry: every one of its routes is `Internal(...)`
    # (orders-svc calls it service-to-service) or the provider webhook --
    # nothing there is a route the browser should ever reach.

    # /api/** load-shedding cap (LLD-01 §6 step 1).
    api_proxy_max_concurrency: int = 100

    redis_session_url: str = "redis://localhost:6379/0"
    # LLD-09 §7: rate-limit buckets, shared across replicas -- a different
    # Redis instance than session storage (redis-cache vs redis-session,
    # ADR-0010), so a cache-only outage never affects sessions.
    redis_cache_url: str = "redis://localhost:6380/0"
    # One BFF replica's own view of how many siblings it has, purely for
    # the §7.2 Redis-outage fallback's "limit / replica_count" math -- not
    # service discovery; nothing here coordinates with other replicas.
    rate_limit_replica_count: int = 1

    # {kid: base64-encoded 32-byte key}; newest-added key encrypts (LLD-01 §5.5).
    session_enc_keys: dict[str, str]
    session_enc_primary_kid: str

    session_idle_standard_s: int = 1800
    session_abs_standard_s: int = 43200
    session_idle_privileged_s: int = 900
    session_abs_privileged_s: int = 14400

    clock_leeway_s: int = 60

    # Identity event consumer (LLD-01 §8: user.disabled -> revoke all
    # sessions within the target). Shared credential pair; per-service
    # RabbitMQ users/vhosts are a deliberate deferral (future ADR-0003 item).
    rabbitmq_host: str = "localhost"
    rabbitmq_port: int = 5672
    rabbitmq_vhost: str = "/"
    rabbitmq_user: str
    rabbitmq_password: str

    @field_validator("session_enc_keys", mode="before")
    @classmethod
    def _parse_json_dict(cls, value: object) -> object:
        return json.loads(value) if isinstance(value, str) else value

    @field_validator("return_to_allowed_prefixes", mode="before")
    @classmethod
    def _parse_json_list(cls, value: object) -> object:
        return json.loads(value) if isinstance(value, str) else value
