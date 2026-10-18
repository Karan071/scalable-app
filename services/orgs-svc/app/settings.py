"""orgs-svc settings (SF_* env vars, HLD §11.4: fail fast on invalid config)."""

import json

from pydantic import field_validator
from sf_common import BaseServiceSettings


class Settings(BaseServiceSettings):
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "scaleforge"
    db_user: str = "svc_orgs"
    db_password: str

    oidc_issuer: str
    oidc_jwks_uri: str

    # Invitation token encryption for the outbox payload (LLD-03 §6.3).
    invitation_enc_keys: dict[str, str]
    invitation_enc_primary_kid: str
    invitation_ttl_days: int = 7

    # Embedded Casbin enforcer (ADR-0005: every service enforces locally).
    authz_reader_db_user: str = "authz_reader"
    authz_reader_db_password: str
    redis_cache_url: str = "redis://localhost:6380/0"

    # Outbox relay (LLD-08 §5, E2.8). See authz-svc/app/settings.py's
    # equivalent comment re: the shared-credential deferral.
    rabbitmq_host: str = "localhost"
    rabbitmq_port: int = 5672
    rabbitmq_vhost: str = "/"
    rabbitmq_user: str
    rabbitmq_password: str

    @field_validator("invitation_enc_keys", mode="before")
    @classmethod
    def _parse_json_dict(cls, value: object) -> object:
        return json.loads(value) if isinstance(value, str) else value
