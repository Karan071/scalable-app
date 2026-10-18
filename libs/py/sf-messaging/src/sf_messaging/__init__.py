"""Shared event envelope, outbox/relay, consumer, retry, and trace propagation primitives."""

from .consumer import InboxConsumer
from .envelope import build_envelope, envelope_id
from .outbox import add as add_to_outbox
from .relay import OutboxRelay

__all__ = ["InboxConsumer", "OutboxRelay", "add_to_outbox", "build_envelope", "envelope_id"]
