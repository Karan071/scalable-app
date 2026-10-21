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
    # Production is always True; local HTTP-only testing (no TLS terminator
    # in front of this process) sets it False so cookies actually round-trip.
    cookie_secure: bool = True

    svc_bff_client_id: str = "sf-svc-bff"
    svc_bff_client_secret: str
    users_svc_base_url: str
    orgs_svc_base_url: str
    authz_svc_base_url: str

    # /api/** load-shedding cap (LLD-01 §6 step 1).
    api_proxy_max_concurrency: int = 100

    redis_session_url: str = "redis://localhost:6379/0"

    # {kid: base64-encoded 32-byte key}; newest-added key encrypts (LLD-01 §5.5).
    session_enc_keys: dict[str, str]
    session_enc_primary_kid: str

    session_idle_standard_s: int = 1800
    session_abs_standard_s: int = 43200
    session_idle_privileged_s: int = 900
    session_abs_privileged_s: int = 14400

    clock_leeway_s: int = 60

    @field_validator("session_enc_keys", mode="before")
    @classmethod
    def _parse_json_dict(cls, value: object) -> object:
        return json.loads(value) if isinstance(value, str) else value

    @field_validator("return_to_allowed_prefixes", mode="before")
    @classmethod
    def _parse_json_list(cls, value: object) -> object:
        return json.loads(value) if isinstance(value, str) else value
