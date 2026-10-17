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

    @field_validator("invitation_enc_keys", mode="before")
    @classmethod
    def _parse_json_dict(cls, value: object) -> object:
        return json.loads(value) if isinstance(value, str) else value
