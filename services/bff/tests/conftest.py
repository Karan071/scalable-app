"""Sets required env vars before `app.main` is imported by any test module."""

import os

os.environ.setdefault("SF_SERVICE_NAME", "bff")
os.environ.setdefault("SF_OIDC_ISSUER", "https://kc.test/realms/scaleforge")
os.environ.setdefault("SF_OIDC_CLIENT_ID", "sf-bff")
os.environ.setdefault("SF_OIDC_CLIENT_SECRET", "test-secret")
os.environ.setdefault("SF_OIDC_REDIRECT_URI", "https://app.test/auth/callback")
os.environ.setdefault("SF_APP_ORIGIN", "https://app.test")
os.environ.setdefault("SF_SVC_BFF_CLIENT_SECRET", "test-svc-secret")
os.environ.setdefault("SF_USERS_SVC_BASE_URL", "http://users-svc.test")
os.environ.setdefault("SF_ORGS_SVC_BASE_URL", "http://orgs-svc.test")
os.environ.setdefault("SF_AUTHZ_SVC_BASE_URL", "http://authz-svc.test")
os.environ.setdefault(
    "SF_SESSION_ENC_KEYS", '{"k1": "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="}'
)
os.environ.setdefault("SF_SESSION_ENC_PRIMARY_KID", "k1")
os.environ.setdefault("SF_RABBITMQ_USER", "scaleforge")
os.environ.setdefault("SF_RABBITMQ_PASSWORD", "rabbitmq_local_dev")
