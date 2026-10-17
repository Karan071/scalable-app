"""Transactional outbox writer (LLD-08 §5.2).

Call ``add()`` in the same transaction as the state change it reports. Each
producing service owns its own ``outbox`` table (schema in LLD-08 §5.1) and
``NOTIFY``-channel name; this module performs no schema setup.

Publishing directly to the broker from a request handler is forbidden
(LLD-08 §5.2) other than the documented BFF audit exception (LLD-04 §2.4).
"""

import re
from datetime import datetime
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

_VALID_CHANNEL = re.compile(r"^[a-z_][a-z0-9_]*$")

_INSERT_SQL = text(
    """
    INSERT INTO outbox (
        id, exchange, routing_key, aggregate_type, aggregate_id,
        aggregate_version, payload, occurred_at
    )
    VALUES (
        :id, :exchange, :routing_key, :aggregate_type, :aggregate_id,
        :aggregate_version, :payload, :occurred_at
    )
    """
).bindparams(bindparam("payload", type_=JSONB))


async def add(
    session: AsyncSession,
    envelope: dict[str, Any],
    *,
    exchange: str,
    routing_key: str,
    aggregate_id: str,
    notify_channel: str,
) -> None:
    if not _VALID_CHANNEL.match(notify_channel):
        raise ValueError(f"not a safe NOTIFY channel name: {notify_channel!r}")

    await session.execute(
        _INSERT_SQL,
        {
            "id": envelope["id"],
            "exchange": exchange,
            "routing_key": routing_key,
            "aggregate_type": envelope["sfaggregatetype"],
            "aggregate_id": aggregate_id,
            "aggregate_version": envelope["sfaggregateversion"],
            "payload": envelope,
            # asyncpg binds timestamptz params as datetimes, not ISO strings;
            # envelope["time"] is the CloudEvents wire format (confirmed
            # against live Postgres, 2026-10-18).
            "occurred_at": datetime.fromisoformat(envelope["time"]),
        },
    )
    # NOTIFY takes no bind parameters; notify_channel is caller-controlled
    # and validated above, never request input.
    await session.execute(text(f"NOTIFY {notify_channel}"))
