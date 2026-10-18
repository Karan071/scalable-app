"""Sets required env vars before `app.main` is imported by any test module."""

import os

os.environ.setdefault("SF_SERVICE_NAME", "authz-svc")
os.environ.setdefault("SF_DB_HOST", "localhost")
os.environ.setdefault("SF_DB_PORT", "5432")
os.environ.setdefault("SF_DB_NAME", "scaleforge")
os.environ.setdefault("SF_DB_USER", "svc_authz")
os.environ.setdefault("SF_DB_PASSWORD", "app_local_dev")
os.environ.setdefault("SF_AUTHZ_READER_DB_USER", "authz_reader")
os.environ.setdefault("SF_AUTHZ_READER_DB_PASSWORD", "app_local_dev")
os.environ.setdefault("SF_OIDC_ISSUER", "https://kc.test/realms/scaleforge")
os.environ.setdefault(
    "SF_OIDC_JWKS_URI", "https://kc.test/realms/scaleforge/protocol/openid-connect/certs"
)
os.environ.setdefault(
    "SF_OIDC_TOKEN_ENDPOINT", "https://kc.test/realms/scaleforge/protocol/openid-connect/token"
)
os.environ.setdefault("SF_SVC_AUTHZ_CLIENT_SECRET", "test-authz-secret")
os.environ.setdefault("SF_ORGS_SVC_BASE_URL", "https://orgs.test")
os.environ.setdefault("SF_REDIS_CACHE_URL", "redis://localhost:6380/0")
os.environ.setdefault("SF_RABBITMQ_USER", "scaleforge")
os.environ.setdefault("SF_RABBITMQ_PASSWORD", "rabbitmq_local_dev")
