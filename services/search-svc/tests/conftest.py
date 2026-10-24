"""Sets required env vars before `app.main` is imported by any test module."""

import os

# Isolated test environment (`make test-env`): never the dev DB, vhost, or
# Redis DBs. No telemetry export: test processes would otherwise report
# into the dev Collector under the dev service names.
os.environ.setdefault("OTEL_SDK_DISABLED", "true")
os.environ.setdefault("SF_SERVICE_NAME", "search-svc")
os.environ.setdefault("SF_DB_HOST", "localhost")
os.environ.setdefault("SF_DB_PORT", "5432")
os.environ.setdefault("SF_DB_NAME", "scaleforge_test")
os.environ.setdefault("SF_DB_USER", "svc_search")
os.environ.setdefault("SF_DB_PASSWORD", "app_local_dev")
os.environ.setdefault("SF_RABBITMQ_VHOST", "scaleforge-test")
os.environ.setdefault("SF_RABBITMQ_USER", "scaleforge")
os.environ.setdefault("SF_RABBITMQ_PASSWORD", "rabbitmq_local_dev")
os.environ.setdefault("SF_CURSOR_SECRET_B64", "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=")
