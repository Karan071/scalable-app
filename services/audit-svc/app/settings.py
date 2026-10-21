"""audit-svc settings (SF_* env vars, HLD §11.4: fail fast on invalid config)."""

from sf_common import BaseServiceSettings


class Settings(BaseServiceSettings):
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "scaleforge"
    db_user: str = "svc_audit"
    db_password: str

    # Read-only enforcer role (ADR-0004 §2) — policy load/reload only.
    authz_reader_db_user: str = "authz_reader"
    authz_reader_db_password: str

    oidc_issuer: str
    oidc_jwks_uri: str

    redis_cache_url: str = "redis://localhost:6380/0"

    # InboxConsumer (LLD-08 §6, E2.8). Shared credential pair; per-service
    # RabbitMQ users/vhosts are a deliberate deferral (future ADR-0003 item).
    rabbitmq_host: str = "localhost"
    rabbitmq_port: int = 5672
    rabbitmq_vhost: str = "/"
    rabbitmq_user: str
    rabbitmq_password: str
