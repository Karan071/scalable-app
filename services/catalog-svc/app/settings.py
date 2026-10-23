"""catalog-svc settings (SF_* env vars, HLD §11.4: fail fast on invalid config)."""

from sf_common import BaseServiceSettings


class Settings(BaseServiceSettings):
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "scaleforge"
    db_user: str = "svc_catalog"
    db_password: str

    oidc_issuer: str
    oidc_jwks_uri: str

    # Embedded Casbin enforcer (ADR-0005: every service enforces locally).
    authz_reader_db_user: str = "authz_reader"
    authz_reader_db_password: str
    redis_cache_url: str = "redis://localhost:6380/0"

    # Outbox relay + inbox consumer (LLD-08 §5-§6, E3.1).
    rabbitmq_host: str = "localhost"
    rabbitmq_port: int = 5672
    rabbitmq_vhost: str = "/"
    rabbitmq_user: str
    rabbitmq_password: str
