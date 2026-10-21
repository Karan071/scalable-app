"""Integration tests for audit-svc event handlers (LLD-04 §2, §6).

Skips if local Postgres is unreachable. Tests call the handler functions
directly (same style as authz-svc's test_event_handlers.py).
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    try:
        from app.db import build_audit_engine, build_audit_session_factory
        from app.settings import Settings
    except Exception as exc:
        pytest.skip(f"could not import app modules: {exc}")
        return

    engine = build_audit_engine(Settings())
    factory = build_audit_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(select(text("1")))
    except Exception as exc:
        await engine.dispose()
        pytest.skip(f"local Postgres not reachable: {exc}")
        return
    try:
        yield factory
    finally:
        await engine.dispose()


async def test_handle_audit_recorded_inserts_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app import event_handlers, repository
    from sf_messaging import build_envelope

    audit_id = uuid.uuid4()
    envelope = build_envelope(
        event_type="audit.recorded",
        source="users-svc",
        subject=str(audit_id),
        data={
            "audit_id": str(audit_id),
            "occurred_at": datetime.now(UTC).isoformat(),
            "actor": {"type": "service", "id": "users-svc"},
            "domain": "platform",
            "action": "user.provisioned",
            "target": {"type": "user", "id": str(uuid.uuid4())},
            "result": "success",
            "source": {"service": "users-svc", "version": "0.1.0"},
            "severity": "info",
        },
        schema_version="1.0",
        aggregate_type="audit_event",
        aggregate_version=1,
    )

    async with session_factory() as session, session.begin():
        await event_handlers.handle_audit_recorded(session, envelope)

    async with session_factory() as session:
        rows = await repository.list_audit_events_platform(session, limit=10)

    ids = [str(r.id) for r in rows]
    assert str(audit_id) in ids


async def test_handle_audit_recorded_idempotent_on_duplicate_audit_id(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two calls with the same audit_id — second must fail with IntegrityError."""
    from app import event_handlers
    from sf_messaging import build_envelope

    audit_id = uuid.uuid4()

    def _envelope() -> dict:
        return build_envelope(
            event_type="audit.recorded",
            source="users-svc",
            subject=str(audit_id),
            data={
                "audit_id": str(audit_id),
                "occurred_at": datetime.now(UTC).isoformat(),
                "actor": {"type": "service", "id": "users-svc"},
                "domain": "platform",
                "action": "user.provisioned",
                "target": {"type": "user", "id": str(uuid.uuid4())},
                "result": "success",
                "source": {"service": "users-svc", "version": "0.1.0"},
                "severity": "info",
            },
            schema_version="1.0",
            aggregate_type="audit_event",
            aggregate_version=1,
        )

    async with session_factory() as session, session.begin():
        await event_handlers.handle_audit_recorded(session, _envelope())

    # Second insert with the same audit_id must be rejected by the PK constraint.
    with pytest.raises(IntegrityError):
        async with session_factory() as session, session.begin():
            await event_handlers.handle_audit_recorded(session, _envelope())


async def test_hash_chain_is_intact_after_multiple_inserts(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app import event_handlers, repository
    from sf_messaging import build_envelope

    inserted_ids = []
    for i in range(3):
        audit_id = uuid.uuid4()
        inserted_ids.append(str(audit_id))
        envelope = build_envelope(
            event_type="audit.recorded",
            source="users-svc",
            subject=str(audit_id),
            data={
                "audit_id": str(audit_id),
                "occurred_at": datetime.now(UTC).isoformat(),
                "actor": {"type": "service", "id": "users-svc"},
                "domain": f"org:{uuid.uuid4()}",
                "action": f"test.action.{i}",
                "target": {"type": "user", "id": str(uuid.uuid4())},
                "result": "success",
                "source": {"service": "users-svc", "version": "0.1.0"},
                "severity": "info",
            },
            schema_version="1.0",
            aggregate_type="audit_event",
            aggregate_version=1,
        )
        async with session_factory() as session, session.begin():
            await event_handlers.handle_audit_recorded(session, envelope)

    async with session_factory() as session:
        ok, broken_at = await repository.verify_chain(session)

    assert ok is True, f"chain broken at seq {broken_at}"


async def test_svc_audit_cannot_update_rows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """svc_audit must not have UPDATE privilege on audit_events (LLD-04 §2.2)."""
    from app import event_handlers
    from sf_messaging import build_envelope

    audit_id = uuid.uuid4()
    envelope = build_envelope(
        event_type="audit.recorded",
        source="users-svc",
        subject=str(audit_id),
        data={
            "audit_id": str(audit_id),
            "occurred_at": datetime.now(UTC).isoformat(),
            "actor": {"type": "service", "id": "users-svc"},
            "domain": "platform",
            "action": "user.provisioned",
            "target": {"type": "user", "id": str(uuid.uuid4())},
            "result": "success",
            "source": {"service": "users-svc", "version": "0.1.0"},
            "severity": "info",
        },
        schema_version="1.0",
        aggregate_type="audit_event",
        aggregate_version=1,
    )

    async with session_factory() as session, session.begin():
        await event_handlers.handle_audit_recorded(session, envelope)

    from sqlalchemy.exc import ProgrammingError

    with pytest.raises((ProgrammingError, Exception)) as exc_info:
        async with session_factory() as session, session.begin():
            await session.execute(
                text("UPDATE audit_events SET action = 'tampered' WHERE id = :id"),
                {"id": str(audit_id)},
            )

    # Either a permission denied (42501) or the trigger (P0001) fires.
    err_str = str(exc_info.value)
    assert "append-only" in err_str or "permission denied" in err_str or "42501" in err_str
