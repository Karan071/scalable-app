"""authz-svc settings (SF_* env vars, HLD §11.4: fail fast on invalid config)."""

from sf_common import BaseServiceSettings


class Settings(BaseServiceSettings):
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "scaleforge"
    db_user: str = "svc_authz"
    db_password: str

    # Read-only enforcer role (ADR-0004 §2) — policy load/reload only, never writes.
    authz_reader_db_user: str = "authz_reader"
    authz_reader_db_password: str

    oidc_issuer: str
    oidc_jwks_uri: str
    oidc_token_endpoint: str

    svc_authz_client_id: str = "sf-svc-authz"
    svc_authz_client_secret: str

    orgs_svc_base_url: str

    # Keycloak Admin REST API access for mfa-required group membership
    # (LLD-01 §3.1, §9, accepted): a dedicated client-credentials client,
    # never the realm admin, distinct from users-svc's own sf-users-admin.
    keycloak_admin_base_url: str
    keycloak_admin_client_id: str = "sf-authz-admin"
    keycloak_admin_client_secret: str

    redis_cache_url: str = "redis://localhost:6380/0"

    # Outbox relay + inbox consumer (LLD-08 §5-§6, E2.8). Shared admin-ish
    # credential pair for now — per-service RabbitMQ users/vhosts are a
    # deliberate, already-agreed deferral to a future ADR-0003 revisit.
    rabbitmq_host: str = "localhost"
    rabbitmq_port: int = 5672
    rabbitmq_vhost: str = "/"
    rabbitmq_user: str
    rabbitmq_password: str
