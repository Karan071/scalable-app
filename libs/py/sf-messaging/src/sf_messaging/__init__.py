"""Shared event envelope, outbox/relay, consumer, retry, and trace propagation primitives."""

from .envelope import build_envelope, envelope_id
from .outbox import add as add_to_outbox

__all__ = ["add_to_outbox", "build_envelope", "envelope_id"]
