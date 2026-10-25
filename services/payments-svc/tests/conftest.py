"""Sets required env vars before `app.main` is imported by any test module."""

import os

os.environ.setdefault("OTEL_SDK_DISABLED", "true")
os.environ.setdefault("SF_SERVICE_NAME", "payments-svc")
os.environ.setdefault("SF_DB_HOST", "localhost")
os.environ.setdefault("SF_DB_PORT", "5432")
os.environ.setdefault("SF_DB_NAME", "scaleforge_test")
os.environ.setdefault("SF_DB_USER", "svc_payments")
os.environ.setdefault("SF_DB_PASSWORD", "app_local_dev")
os.environ.setdefault("SF_OIDC_ISSUER", "https://kc.test/realms/scaleforge")
os.environ.setdefault(
    "SF_OIDC_JWKS_URI", "https://kc.test/realms/scaleforge/protocol/openid-connect/certs"
)
os.environ.setdefault("SF_RABBITMQ_VHOST", "scaleforge-test")
os.environ.setdefault("SF_RABBITMQ_USER", "scaleforge")
os.environ.setdefault("SF_RABBITMQ_PASSWORD", "rabbitmq_local_dev")
os.environ.setdefault("SF_PROVIDER_BASE_URL", "https://provider.test")
os.environ.setdefault("SF_WEBHOOK_SECRETS", "test-webhook-secret")
