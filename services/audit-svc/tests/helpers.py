"""Shared test helpers for audit-svc."""

import uuid
from datetime import UTC, datetime
from uuid import UUID

from sf_authz import Principal


def user_principal(sf_uid: UUID, *, acr: int = 0) -> Principal:
    return Principal(
        subject=f"user:{sf_uid}",
        audiences=("scaleforge-api",),
        claims={"sf_uid": str(sf_uid), "acr": acr},
    )


def override_all_as(app, sf_uid: UUID, *, acr: int = 0) -> None:
    """Overrides every auth dependency in ``app.routes`` to resolve to the same actor."""
    from app import routes

    principal = user_principal(sf_uid, acr=acr)
    for name in (
        "require_read_org_audit",
        "require_read_platform_audit",
        "require_verify_chain",
    ):
        app.dependency_overrides[getattr(routes, name)] = lambda principal=principal: principal


def make_audit_data(
    *,
    audit_id: uuid.UUID | None = None,
    domain: str = "platform",
    action: str = "user.provisioned",
    actor_id: str | None = None,
    result: str = "success",
    severity: str = "info",
    occurred_at: datetime | None = None,
) -> dict:
    """Builds a minimal ``audit.recorded`` envelope data payload."""
    return {
        "audit_id": str(audit_id or uuid.uuid4()),
        "occurred_at": (occurred_at or datetime.now(UTC)).isoformat(),
        "actor": {"type": "service", "id": actor_id or str(uuid.uuid4())},
        "domain": domain,
        "action": action,
        "target": {"type": "user", "id": str(uuid.uuid4())},
        "result": result,
        "source": {"service": "users-svc", "version": "0.1.0"},
        "severity": severity,
    }


async def insert_audit_event_directly(
    *,
    audit_id: uuid.UUID | None = None,
    domain: str = "platform",
    action: str = "user.provisioned",
    actor_id: str | None = None,
    result: str = "success",
    severity: str = "info",
) -> None:
    from app.db import build_audit_engine, build_audit_session_factory
    from app.settings import Settings

    settings = Settings()
    engine = build_audit_engine(settings)
    factory = build_audit_session_factory(engine)
    try:
        from app import repository

        async with factory() as session, session.begin():
            await repository.insert_audit_event(
                session,
                audit_id=audit_id or uuid.uuid4(),
                occurred_at=datetime.now(UTC),
                actor={"type": "service", "id": actor_id or str(uuid.uuid4())},
                on_behalf_of=None,
                domain=domain,
                action=action,
                target={"type": "user", "id": str(uuid.uuid4())},
                result=result,
                reason=None,
                changes=None,
                source={"service": "users-svc", "version": "0.1.0"},
                correlation_id=None,
                trace_id=None,
                client=None,
                severity=severity,
            )
    finally:
        await engine.dispose()
