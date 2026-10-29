"""Shared event envelope, outbox/relay, consumer, retry, and trace propagation primitives."""

from .audit import build_audit_data, record_audit
from .consumer import DEFAULT_HANDLER_CONCURRENCY, InboxConsumer
from .envelope import build_envelope, envelope_id
from .outbox import add as add_to_outbox
from .relay import RELAY_POOL_SIZE, OutboxRelay
from .retry import RETRY_TIERS, TransientError, is_transient, queue_name_for_tier

__all__ = [
    "DEFAULT_HANDLER_CONCURRENCY",
    "RETRY_TIERS",
    "InboxConsumer",
    "OutboxRelay",
    "RELAY_POOL_SIZE",
    "TransientError",
    "add_to_outbox",
    "build_audit_data",
    "build_envelope",
    "envelope_id",
    "is_transient",
    "queue_name_for_tier",
    "record_audit",
]
