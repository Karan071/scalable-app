"""Messaging metrics (LLD-08 §13, LLD-10 §7).

Instruments come from the global meter provider via OTel's proxy meter, so
they bind to whatever ``sf_common.init_telemetry`` installs at startup, and
are no-ops if nothing does.

Labels are bounded: ``service``/``consumer`` are per-deployment constants,
``type`` is an event type from the catalog (LLD-08 §4), ``result`` is one
of the enums below. Never ids (LLD-10 §6).
"""

from collections.abc import Iterable
from datetime import UTC, datetime

from opentelemetry import metrics
from opentelemetry.metrics import CallbackOptions, Observation

_meter = metrics.get_meter("sf_messaging")

# Count units are annotations ("{row}"), not "1": OTLP -> Prometheus
# translation turns a "1"-unit gauge into "<name>_ratio".

# confirmed | no_subscriber | returned | nacked | error. `no_subscriber` is
# an sf.events message nothing is bound to yet: done, not retried
# (LLD-08 §5.3); `returned` is the same for sf.commands, which is retried.
outbox_publish_total = _meter.create_counter(
    "outbox_publish_total", unit="{message}", description="Outbox rows the relay tried to publish"
)
outbox_publish_latency_seconds = _meter.create_histogram(
    "outbox_publish_latency_seconds",
    unit="s",
    description="Producer commit (occurred_at) to broker confirm",
)
# processed | duplicate | dead_lettered (no retry tiers until M4, so any
# handler failure nacks without requeue straight to the queue's DLQ).
consumer_messages_total = _meter.create_counter(
    "consumer_messages_total", unit="{message}", description="Messages an inbox consumer finished"
)
consumer_processing_duration_seconds = _meter.create_histogram(
    "consumer_processing_duration_seconds",
    unit="s",
    description="Inbox consumer handling time, receive to ack/nack",
)

# service -> (pending row count, oldest unpublished occurred_at), refreshed
# by each relay's own loop.
_backlog: dict[str, tuple[int, datetime | None]] = {}


def record_backlog(service: str, *, pending: int, oldest_unpublished_at: datetime | None) -> None:
    _backlog[service] = (pending, oldest_unpublished_at)


def _observe_pending(_options: CallbackOptions) -> Iterable[Observation]:
    for service, (pending, _) in list(_backlog.items()):
        yield Observation(pending, {"service": service})


def _observe_oldest_age(_options: CallbackOptions) -> Iterable[Observation]:
    # Age is computed at observation time from the cached timestamp, not
    # cached itself: if the relay loop dies with rows pending, the age keeps
    # growing instead of freezing, so the alert (LLD-08 §11, > 60 s) still
    # fires. (A loop that dies with an empty outbox reports 0 until restart.)
    now = datetime.now(UTC)
    for service, (_, oldest) in list(_backlog.items()):
        age = (now - oldest).total_seconds() if oldest is not None else 0.0
        yield Observation(max(age, 0.0), {"service": service})


_meter.create_observable_gauge(
    "outbox_pending",
    callbacks=[_observe_pending],
    unit="{row}",
    description="Unpublished outbox rows",
)
_meter.create_observable_gauge(
    "outbox_oldest_unpublished_age_seconds",
    callbacks=[_observe_oldest_age],
    unit="s",
    description="Age of the oldest unpublished outbox row (alert > 60 s, LLD-08 §11)",
)
