"""Sets required env vars before `app.main` is imported by any test module."""

import os

# Isolated test environment (`make test-env`): never the dev DB, vhost, or
# Redis DBs. Redis pub/sub (authz:policy) is global across DB indexes, so
# policy-change notices still reach dev watchers -- harmless, they reload.
# No telemetry export: test processes would otherwise report into the dev
# Collector under the dev service names (and could trip dev alerts).
os.environ.setdefault("OTEL_SDK_DISABLED", "true")
os.environ.setdefault("SF_SERVICE_NAME", "orgs-svc")
os.environ.setdefault("SF_DB_HOST", "localhost")
os.environ.setdefault("SF_DB_PORT", "5432")
os.environ.setdefault("SF_DB_NAME", "scaleforge_test")
os.environ.setdefault("SF_DB_USER", "svc_orgs")
os.environ.setdefault("SF_DB_PASSWORD", "app_local_dev")
os.environ.setdefault("SF_OIDC_ISSUER", "https://kc.test/realms/scaleforge")
os.environ.setdefault(
    "SF_OIDC_JWKS_URI", "https://kc.test/realms/scaleforge/protocol/openid-connect/certs"
)
os.environ.setdefault(
    "SF_INVITATION_ENC_KEYS", '{"k1": "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="}'
)
os.environ.setdefault("SF_INVITATION_ENC_PRIMARY_KID", "k1")
os.environ.setdefault("SF_AUTHZ_READER_DB_USER", "authz_reader")
os.environ.setdefault("SF_AUTHZ_READER_DB_PASSWORD", "app_local_dev")
os.environ.setdefault("SF_REDIS_CACHE_URL", "redis://localhost:6380/1")
os.environ.setdefault("SF_RABBITMQ_VHOST", "scaleforge-test")
os.environ.setdefault("SF_RABBITMQ_USER", "scaleforge")
os.environ.setdefault("SF_RABBITMQ_PASSWORD", "rabbitmq_local_dev")
