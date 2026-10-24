"""inventory-svc settings (SF_* env vars, HLD §11.4: fail fast on invalid config)."""

from sf_common import BaseServiceSettings


class Settings(BaseServiceSettings):
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "scaleforge"
    db_user: str = "svc_inventory"
    db_password: str

    oidc_issuer: str
    oidc_jwks_uri: str

    # Embedded Casbin enforcer (ADR-0005: every service enforces locally).
    authz_reader_db_user: str = "authz_reader"
    authz_reader_db_password: str
    redis_cache_url: str = "redis://localhost:6380/0"

    # Outbox relay + inbox consumers (LLD-08 §5-§6): product.updated.v1 (new
    # SKUs) from catalog-svc, and the commit/release/restock commands
    # orders-svc will publish once it exists.
    rabbitmq_host: str = "localhost"
    rabbitmq_port: int = 5672
    rabbitmq_vhost: str = "/"
    rabbitmq_user: str
    rabbitmq_password: str

    # LLD-07 §6: default reservation TTL, configurable per store between
    # 300 and 3600s. Per-store override isn't wired up yet (no settings
    # projection from orgs-svc exists for inventory-svc) -- this fixed
    # default is this epic's own deferred item.
    reservation_ttl_seconds: int = 900
    sweeper_interval_seconds: float = 5.0
    sweeper_batch_size: int = 100
