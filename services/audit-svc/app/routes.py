"""audit-svc HTTP routes (LLD-04 §2.5).

Query endpoints:
  GET /v1/orgs/{org_id}/audit-events      — org ``audit_log:read`` (org_admin)
  GET /v1/platform/audit-events           — platform ``audit_log:read``, acr ≥ 2
  GET /v1/platform/audit-chain:verify     — platform ``audit_chain:verify`` (super_user)

Reading platform audit data is itself audited via a direct ``insert_audit_event``
call in the handler — not through the outbox/relay path — since audit-svc is
its own sink and a self-loop via RabbitMQ would add latency and complexity for
no benefit.

Keyset cursor encodes ``{occurred_at: ISO-8601, seq: int}`` as a base64 JSON
blob. The 92-day window is enforced server-side (LLD-04 §2.5).
"""

import base64
import json
import uuid
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sf_authz import Authorize, Principal
from sqlalchemy.ext.asyncio import AsyncSession

from . import repository
from .db import get_session

router = APIRouter()

_MAX_WINDOW_DAYS = 92
_DEFAULT_LIMIT = 50
_MAX_LIMIT = 200


def _org_domain(request: Request) -> str:
    return f"org:{request.path_params['org_id']}"


require_read_org_audit = Authorize("audit_log", "read", domain=_org_domain)
require_read_platform_audit = Authorize("audit_log", "read", domain="platform", min_acr=2)
require_verify_chain = Authorize("audit_chain", "verify", domain="platform")


class AuditEventOut(BaseModel):
    id: uuid.UUID
    seq: int
    occurred_at: datetime
    recorded_at: datetime
    actor: dict
    on_behalf_of: uuid.UUID | None
    domain: str
    action: str
    target: dict
    result: str
    reason: str | None
    changes: dict | None
    source: dict
    correlation_id: str | None
    trace_id: str | None
    client: dict | None
    severity: str

    model_config = {"from_attributes": True}


class AuditEventPage(BaseModel):
    items: list[AuditEventOut]
    next_cursor: str | None


class ChainVerifyOut(BaseModel):
    ok: bool
    broken_at_seq: int | None


def _decode_cursor(cursor: str) -> tuple[datetime, int]:
    try:
        raw = json.loads(base64.urlsafe_b64decode(cursor + "==").decode())
        ts = datetime.fromisoformat(raw["occurred_at"])
        return ts, int(raw["seq"])
    except Exception as exc:
        raise HTTPException(status_code=422, detail="invalid cursor") from exc


def _encode_cursor(occurred_at: datetime, seq: int) -> str:
    raw = json.dumps({"occurred_at": occurred_at.isoformat(), "seq": seq})
    return base64.urlsafe_b64encode(raw.encode()).rstrip(b"=").decode()


def _window_bounds(
    from_ts: datetime | None,
    to_ts: datetime | None,
) -> tuple[datetime | None, datetime | None]:
    """Clamps the query window to at most 92 days."""
    now = datetime.now(UTC)
    effective_to = min(to_ts, now) if to_ts else now
    effective_from = from_ts or (effective_to - timedelta(days=_MAX_WINDOW_DAYS))
    if (effective_to - effective_from).days > _MAX_WINDOW_DAYS:
        effective_from = effective_to - timedelta(days=_MAX_WINDOW_DAYS)
    return effective_from, effective_to


@router.get("/v1/orgs/{org_id}/audit-events")
async def list_org_audit_events(
    org_id: UUID,
    session: AsyncSession = Depends(get_session),
    _principal: Principal = Depends(require_read_org_audit),
    action: str | None = Query(None),
    actor: str | None = Query(None),
    from_ts: datetime | None = Query(None, alias="from"),
    to_ts: datetime | None = Query(None, alias="to"),
    cursor: str | None = Query(None),
    limit: int = Query(_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
) -> AuditEventPage:
    from_dt, to_dt = _window_bounds(from_ts, to_ts)
    cursor_occurred_at, cursor_seq = None, None
    if cursor:
        cursor_occurred_at, cursor_seq = _decode_cursor(cursor)

    rows = await repository.list_audit_events(
        session,
        domain=f"org:{org_id}",
        action=action,
        actor_id=actor,
        from_ts=from_dt,
        to_ts=to_dt,
        cursor_occurred_at=cursor_occurred_at,
        cursor_seq=cursor_seq,
        limit=limit,
    )

    next_cursor = None
    if len(rows) == limit:
        last = rows[-1]
        next_cursor = _encode_cursor(last.occurred_at, last.seq)

    return AuditEventPage(
        items=[AuditEventOut.model_validate(r) for r in rows],
        next_cursor=next_cursor,
    )


@router.get("/v1/platform/audit-events")
async def list_platform_audit_events(
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_read_platform_audit),
    action: str | None = Query(None),
    actor: str | None = Query(None),
    from_ts: datetime | None = Query(None, alias="from"),
    to_ts: datetime | None = Query(None, alias="to"),
    cursor: str | None = Query(None),
    limit: int = Query(_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
) -> AuditEventPage:
    from_dt, to_dt = _window_bounds(from_ts, to_ts)
    cursor_occurred_at, cursor_seq = None, None
    if cursor:
        cursor_occurred_at, cursor_seq = _decode_cursor(cursor)

    rows = await repository.list_audit_events_platform(
        session,
        action=action,
        actor_id=actor,
        from_ts=from_dt,
        to_ts=to_dt,
        cursor_occurred_at=cursor_occurred_at,
        cursor_seq=cursor_seq,
        limit=limit,
    )

    # Reading audit data is itself audited (LLD-04 §2.5).
    sf_uid = principal.claim("sf_uid")
    if sf_uid is not None:
        try:
            import uuid as _uuid
            from datetime import UTC as _UTC

            await repository.insert_audit_event(
                session,
                audit_id=_uuid.uuid4(),
                occurred_at=datetime.now(_UTC),
                actor={"type": "user", "id": str(sf_uid)},
                on_behalf_of=None,
                domain="platform",
                action="audit.queried",
                target={"type": "audit_log", "id": "platform"},
                result="success",
                reason=None,
                changes=None,
                source={"service": "audit-svc", "version": "0.1.0"},
                correlation_id=request.headers.get("x-correlation-id"),
                trace_id=request.headers.get("traceparent"),
                client=None,
                severity="info",
            )
        except Exception:
            pass  # best-effort; never fail the query because of its own audit record

    next_cursor = None
    if len(rows) == limit:
        last = rows[-1]
        next_cursor = _encode_cursor(last.occurred_at, last.seq)

    return AuditEventPage(
        items=[AuditEventOut.model_validate(r) for r in rows],
        next_cursor=next_cursor,
    )


@router.get("/v1/platform/audit-chain:verify")
async def verify_audit_chain(
    session: AsyncSession = Depends(get_session),
    _principal: Principal = Depends(require_verify_chain),
) -> ChainVerifyOut:
    ok, broken_at_seq = await repository.verify_chain(session)
    return ChainVerifyOut(ok=ok, broken_at_seq=broken_at_seq)
