"""Direct DB access for the ``audit`` schema (LLD-04 §2)."""

import hashlib
import json
import uuid
from datetime import UTC, datetime

from sqlalchemy import desc, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from .models import AuditEvent

# Advisory lock ID used to serialise hash-chain writes (LLD-04 §2.2).
# Transaction-level: auto-released on commit/rollback; one holder at a time.
_CHAIN_LOCK_ID = 7_777_777_777

# The genesis prev_hash: 32 zero bytes (no predecessor row).
_ZERO_HASH = b"\x00" * 32


def _canonical_json(event: dict) -> bytes:
    """Deterministic JSON encoding for the hash input."""
    return json.dumps(event, sort_keys=True, separators=(",", ":"), default=str).encode()


def _compute_hash(prev_hash: bytes, event: dict) -> bytes:
    return hashlib.sha256(prev_hash + _canonical_json(event)).digest()


async def acquire_chain_lock(session: AsyncSession) -> None:
    """Acquires a transaction-level advisory lock — serialises all chain writes."""
    await session.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": _CHAIN_LOCK_ID})


async def get_chain_tip(session: AsyncSession) -> tuple[bytes, int]:
    """Returns ``(prev_hash, last_seq)`` of the latest row, or the genesis values."""
    stmt = select(AuditEvent.hash, AuditEvent.seq).order_by(desc(AuditEvent.seq)).limit(1)
    row = (await session.execute(stmt)).first()
    if row is None:
        return _ZERO_HASH, 0
    return bytes(row.hash), row.seq


async def insert_audit_event(
    session: AsyncSession,
    *,
    audit_id: uuid.UUID,
    occurred_at: datetime,
    actor: dict,
    on_behalf_of: uuid.UUID | None,
    domain: str,
    action: str,
    target: dict,
    result: str,
    reason: str | None,
    changes: dict | None,
    source: dict,
    correlation_id: str | None,
    trace_id: str | None,
    client: dict | None,
    severity: str,
) -> AuditEvent:
    """Acquires the chain lock, computes the hash, and inserts one audit row.

    Must be called inside an open transaction (InboxConsumer or direct).
    Returns the inserted row. Raises ``IntegrityError`` on duplicate ``audit_id``.
    """
    await acquire_chain_lock(session)
    prev_hash, _ = await get_chain_tip(session)

    # The canonical event dict for hashing covers all immutable fields.
    event_dict = {
        "audit_id": str(audit_id),
        "occurred_at": occurred_at.isoformat(),
        "actor": actor,
        "domain": domain,
        "action": action,
        "target": target,
        "result": result,
        "source": source,
        "severity": severity,
    }
    new_hash = _compute_hash(prev_hash, event_dict)

    row = AuditEvent(
        id=audit_id,
        occurred_at=occurred_at,
        recorded_at=datetime.now(UTC),
        actor=actor,
        on_behalf_of=on_behalf_of,
        domain=domain,
        action=action,
        target=target,
        result=result,
        reason=reason,
        changes=changes,
        source=source,
        correlation_id=correlation_id,
        trace_id=trace_id,
        client=client,
        severity=severity,
        prev_hash=prev_hash,
        hash=new_hash,
    )
    session.add(row)
    await session.flush()
    return row


async def list_audit_events(
    session: AsyncSession,
    *,
    domain: str,
    action: str | None = None,
    actor_id: str | None = None,
    from_ts: datetime | None = None,
    to_ts: datetime | None = None,
    cursor_occurred_at: datetime | None = None,
    cursor_seq: int | None = None,
    limit: int = 50,
) -> list[AuditEvent]:
    """Keyset pagination over ``(occurred_at DESC, seq DESC)`` (LLD-04 §2.5).

    Maximum window of 92 days is enforced by the caller (routes.py).
    """
    stmt = select(AuditEvent).where(AuditEvent.domain == domain)

    if action is not None:
        stmt = stmt.where(AuditEvent.action == action)
    if actor_id is not None:
        stmt = stmt.where(AuditEvent.actor["id"].as_string() == actor_id)
    if from_ts is not None:
        stmt = stmt.where(AuditEvent.occurred_at >= from_ts)
    if to_ts is not None:
        stmt = stmt.where(AuditEvent.occurred_at <= to_ts)

    # Keyset: rows strictly before the cursor position.
    if cursor_occurred_at is not None and cursor_seq is not None:
        stmt = stmt.where(
            (AuditEvent.occurred_at < cursor_occurred_at)
            | ((AuditEvent.occurred_at == cursor_occurred_at) & (AuditEvent.seq < cursor_seq))
        )

    stmt = stmt.order_by(desc(AuditEvent.occurred_at), desc(AuditEvent.seq)).limit(limit)
    return list((await session.execute(stmt)).scalars().all())


async def list_audit_events_platform(
    session: AsyncSession,
    *,
    action: str | None = None,
    actor_id: str | None = None,
    from_ts: datetime | None = None,
    to_ts: datetime | None = None,
    cursor_occurred_at: datetime | None = None,
    cursor_seq: int | None = None,
    limit: int = 50,
) -> list[AuditEvent]:
    """Same as ``list_audit_events`` but without a domain filter (platform query)."""
    stmt = select(AuditEvent)

    if action is not None:
        stmt = stmt.where(AuditEvent.action == action)
    if actor_id is not None:
        stmt = stmt.where(AuditEvent.actor["id"].as_string() == actor_id)
    if from_ts is not None:
        stmt = stmt.where(AuditEvent.occurred_at >= from_ts)
    if to_ts is not None:
        stmt = stmt.where(AuditEvent.occurred_at <= to_ts)

    if cursor_occurred_at is not None and cursor_seq is not None:
        stmt = stmt.where(
            (AuditEvent.occurred_at < cursor_occurred_at)
            | ((AuditEvent.occurred_at == cursor_occurred_at) & (AuditEvent.seq < cursor_seq))
        )

    stmt = stmt.order_by(desc(AuditEvent.occurred_at), desc(AuditEvent.seq)).limit(limit)
    return list((await session.execute(stmt)).scalars().all())


async def verify_chain(session: AsyncSession) -> tuple[bool, int | None]:
    """Reads all rows in seq order and recomputes hashes.

    Returns ``(True, None)`` if the chain is intact, or ``(False, broken_seq)``
    at the first mismatch. Intended for the verification endpoint; not suitable
    for tables with hundreds of millions of rows (deferred optimisation).
    """
    stmt = select(AuditEvent).order_by(AuditEvent.seq)
    rows = list((await session.execute(stmt)).scalars().all())

    expected_prev = _ZERO_HASH
    for row in rows:
        event_dict = {
            "audit_id": str(row.id),
            "occurred_at": row.occurred_at.isoformat(),
            "actor": row.actor,
            "domain": row.domain,
            "action": row.action,
            "target": row.target,
            "result": row.result,
            "source": row.source,
            "severity": row.severity,
        }
        expected_hash = _compute_hash(expected_prev, event_dict)
        if bytes(row.prev_hash) != expected_prev or bytes(row.hash) != expected_hash:
            return False, row.seq
        expected_prev = bytes(row.hash)

    return True, None
