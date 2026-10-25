"""Sets required env vars before `app.main` is imported by any test module."""

import os

os.environ.setdefault("OTEL_SDK_DISABLED", "true")
os.environ.setdefault("SF_SERVICE_NAME", "mock-payment-provider")
os.environ.setdefault("SF_WEBHOOK_SECRET", "test-webhook-secret")
os.environ.setdefault("SF_WEBHOOK_TARGET_URL", "http://unused.test/webhooks/payments/mock")
