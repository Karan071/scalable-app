"""Sets required env vars before `app.main` is imported by any test module."""

import os

# Isolated test environment (`make test-env`): never the dev DB, vhost, or
# Redis DBs. Redis pub/sub (authz:policy) is global across DB indexes, so
# policy-change notices still reach dev watchers -- harmless, they reload.
# No telemetry export: test processes would otherwise report into the dev
# Collector under the dev service names (and could trip dev alerts).
os.environ.setdefault("OTEL_SDK_DISABLED", "true")
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
os.environ.setdefault("SF_CATALOG_SVC_BASE_URL", "http://catalog-svc.test")
os.environ.setdefault("SF_SEARCH_SVC_BASE_URL", "http://search-svc.test")
os.environ.setdefault("SF_CART_SVC_BASE_URL", "http://cart-svc.test")
os.environ.setdefault("SF_INVENTORY_SVC_BASE_URL", "http://inventory-svc.test")
os.environ.setdefault("SF_ORDERS_SVC_BASE_URL", "http://orders-svc.test")
os.environ.setdefault(
    "SF_SESSION_ENC_KEYS", '{"k1": "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="}'
)
os.environ.setdefault("SF_SESSION_ENC_PRIMARY_KID", "k1")
os.environ.setdefault("SF_REDIS_SESSION_URL", "redis://localhost:6379/1")
os.environ.setdefault("SF_RABBITMQ_VHOST", "scaleforge-test")
os.environ.setdefault("SF_RABBITMQ_USER", "scaleforge")
os.environ.setdefault("SF_RABBITMQ_PASSWORD", "rabbitmq_local_dev")
