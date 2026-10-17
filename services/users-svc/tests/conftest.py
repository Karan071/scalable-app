"""Sets required env vars before `app.main` is imported by any test module."""

import os

os.environ.setdefault("SF_SERVICE_NAME", "users-svc")
os.environ.setdefault("SF_DB_HOST", "localhost")
os.environ.setdefault("SF_DB_PORT", "5432")
os.environ.setdefault("SF_DB_NAME", "scaleforge")
os.environ.setdefault("SF_DB_USER", "svc_users")
os.environ.setdefault("SF_DB_PASSWORD", "app_local_dev")
os.environ.setdefault("SF_OIDC_ISSUER", "https://kc.test/realms/scaleforge")
os.environ.setdefault(
    "SF_OIDC_JWKS_URI", "https://kc.test/realms/scaleforge/protocol/openid-connect/certs"
)
os.environ.setdefault("SF_KEYCLOAK_ADMIN_BASE_URL", "https://kc.test/admin/realms/scaleforge")
os.environ.setdefault("SF_KEYCLOAK_ADMIN_CLIENT_SECRET", "test-admin-secret")
