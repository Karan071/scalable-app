import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sf_messaging.metrics import record_backlog


def test_backlog_gauges_report_pending_and_oldest_age(read_metric) -> None:  # type: ignore[no-untyped-def]
    service = f"svc-{uuid4().hex[:8]}"
    record_backlog(
        service, pending=7, oldest_unpublished_at=datetime.now(UTC) - timedelta(minutes=2)
    )

    assert read_metric("outbox_pending", service=service) == 7
    assert read_metric("outbox_oldest_unpublished_age_seconds", service=service) >= 120


def test_oldest_age_keeps_growing_after_the_relay_stops_refreshing(read_metric) -> None:  # type: ignore[no-untyped-def]
    """A relay loop that died with rows pending must still trip the > 60 s
    alert (LLD-08 §11): the age is derived at read time, not cached."""
    service = f"svc-{uuid4().hex[:8]}"
    record_backlog(service, pending=1, oldest_unpublished_at=datetime.now(UTC))

    first = read_metric("outbox_oldest_unpublished_age_seconds", service=service)
    time.sleep(0.2)
    second = read_metric("outbox_oldest_unpublished_age_seconds", service=service)

    assert second > first


def test_empty_outbox_reports_zero_age(read_metric) -> None:  # type: ignore[no-untyped-def]
    service = f"svc-{uuid4().hex[:8]}"
    record_backlog(service, pending=0, oldest_unpublished_at=None)

    assert read_metric("outbox_pending", service=service) == 0
    assert read_metric("outbox_oldest_unpublished_age_seconds", service=service) == 0
