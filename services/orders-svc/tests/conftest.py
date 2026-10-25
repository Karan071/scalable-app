"""Sets required env vars before `app.main` is imported by any test module."""

import os

# Isolated test environment (`make test-env`): never the dev DB, vhost, or
# Redis DBs. No telemetry export: test processes would otherwise report
# into the dev Collector under the dev service names.
os.environ.setdefault("OTEL_SDK_DISABLED", "true")
os.environ.setdefault("SF_SERVICE_NAME", "orders-svc")
os.environ.setdefault("SF_DB_HOST", "localhost")
os.environ.setdefault("SF_DB_PORT", "5432")
os.environ.setdefault("SF_DB_NAME", "scaleforge_test")
os.environ.setdefault("SF_DB_USER", "svc_orders")
os.environ.setdefault("SF_DB_PASSWORD", "app_local_dev")
os.environ.setdefault("SF_OIDC_ISSUER", "https://kc.test/realms/scaleforge")
os.environ.setdefault(
    "SF_OIDC_JWKS_URI", "https://kc.test/realms/scaleforge/protocol/openid-connect/certs"
)
os.environ.setdefault(
    "SF_OIDC_TOKEN_ENDPOINT", "https://kc.test/realms/scaleforge/protocol/openid-connect/token"
)
os.environ.setdefault("SF_AUTHZ_READER_DB_USER", "authz_reader")
os.environ.setdefault("SF_AUTHZ_READER_DB_PASSWORD", "app_local_dev")
os.environ.setdefault("SF_REDIS_CACHE_URL", "redis://localhost:6380/1")
os.environ.setdefault("SF_RABBITMQ_VHOST", "scaleforge-test")
os.environ.setdefault("SF_RABBITMQ_USER", "scaleforge")
os.environ.setdefault("SF_RABBITMQ_PASSWORD", "rabbitmq_local_dev")
os.environ.setdefault("SF_CART_SVC_BASE_URL", "https://cart.test")
os.environ.setdefault("SF_CATALOG_SVC_BASE_URL", "https://catalog.test")
os.environ.setdefault("SF_INVENTORY_SVC_BASE_URL", "https://inventory.test")
os.environ.setdefault("SF_PAYMENTS_SVC_BASE_URL", "https://payments.test")
os.environ.setdefault("SF_SVC_ORDERS_CLIENT_SECRET", "unused-in-tests")
