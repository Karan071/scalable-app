"""orders-svc settings (SF_* env vars, HLD §11.4: fail fast on invalid config)."""

from sf_common import BaseServiceSettings


class Settings(BaseServiceSettings):
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "scaleforge"
    db_user: str = "svc_orders"
    db_password: str

    oidc_issuer: str
    oidc_jwks_uri: str
    oidc_token_endpoint: str

    # Embedded Casbin enforcer (ADR-0005: every service enforces locally).
    authz_reader_db_user: str = "authz_reader"
    authz_reader_db_password: str
    redis_cache_url: str = "redis://localhost:6380/0"

    # Outbox relay + inbox consumer (LLD-08 §5-§6).
    rabbitmq_host: str = "localhost"
    rabbitmq_port: int = 5672
    rabbitmq_vhost: str = "/"
    rabbitmq_user: str
    rabbitmq_password: str

    # The checkout saga's synchronous participants (LLD-06 §5.1): all four
    # exist as of E3.6. One service identity (sf-svc-orders) covers every
    # scope at once (cart:read, catalog:quotes, inventory:reservations:*,
    # payments:authorize).
    cart_svc_base_url: str
    catalog_svc_base_url: str
    inventory_svc_base_url: str
    payments_svc_base_url: str = "http://localhost:8110"
    svc_orders_client_id: str = "sf-svc-orders"
    svc_orders_client_secret: str

    # LLD-06 §5.2: "deadline_at (created + 15 min)".
    saga_deadline_seconds: int = 900
    # LLD-07 §6: the reservation TTL orders-svc asks inventory-svc for.
    reservation_ttl_seconds: int = 900
    recovery_interval_seconds: float = 2.0
    recovery_batch_size: int = 50
