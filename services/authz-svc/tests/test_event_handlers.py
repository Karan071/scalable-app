"""One test per LLD-02 §9 row, calling ``event_handlers.handle_*`` directly
against the real local Postgres (skip if unreachable) — same direct-call
style as ``test_escalation.py``."""

from collections.abc import AsyncIterator
from uuid import UUID, uuid4, uuid7

import pytest
import pytest_asyncio
from sf_messaging import build_envelope
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    try:
        from app.db import build_authz_engine, build_authz_session_factory
        from app.settings import Settings
    except Exception as exc:
        pytest.skip(f"could not import app modules: {exc}")
        return

    engine = build_authz_engine(Settings())
    factory = build_authz_session_factory(engine)
    try:
        async with factory() as session:
            await session.execute(select(1))
    except Exception as exc:
        await engine.dispose()
        pytest.skip(f"local Postgres not reachable: {exc}")
        return
    try:
        yield factory
    finally:
        await engine.dispose()


def _org_membership_envelope(
    *, event_type: str, membership_id: UUID, org_id: UUID, user_id: UUID, **extra: object
) -> dict:
    data = {
        "membership_id": str(membership_id),
        "org_id": str(org_id),
        "user_id": str(user_id),
        **extra,
    }
    return build_envelope(
        event_type=event_type,
        source="orgs-svc",
        subject=str(membership_id),
        data=data,
        schema_version="1.0",
        aggregate_type="membership",
        aggregate_version=1,
    )


async def _run(session_factory, handler, envelope: dict) -> int | None:
    async with session_factory() as session, session.begin():
        return await handler(session, envelope)


async def test_membership_created_grants_roles_unconditionally(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app import event_handlers, repository

    org_id, membership_id, user_id, inviter = uuid7(), uuid7(), uuid7(), uuid7()
    envelope = _org_membership_envelope(
        event_type="membership.created",
        membership_id=membership_id,
        org_id=org_id,
        user_id=user_id,
        roles_requested=["org_admin"],
        invited_by=str(inviter),
    )

    version = await _run(session_factory, event_handlers.handle_membership_created, envelope)
    assert version is not None
    async with session_factory() as session:
        rows = await repository.list_role_assignments(session, domain=f"org:{org_id}")
        assert len(rows) == 1
        assert rows[0].role_id == "org_admin"
        assert rows[0].status == "active"


async def test_membership_disabled_suspends_only_membership_disabled_reason(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app import event_handlers, repository

    org_id, membership_id, user_id = uuid7(), uuid7(), uuid7()

    async with session_factory() as session, session.begin():
        await repository.grant_role_unconditional(
            session,
            user_id=user_id,
            role_id="org_admin",
            domain=f"org:{org_id}",
            org_id=org_id,
            membership_id=membership_id,
            granted_by=user_id,
            reason=None,
        )
        await repository.grant_role_unconditional(
            session,
            user_id=user_id,
            role_id="catalog_manager",
            domain=f"org:{org_id}",
            org_id=org_id,
            membership_id=membership_id,
            granted_by=user_id,
            reason=None,
        )
        # Simulate org_admin already suspended for a *different* reason
        # (user_disabled) -- membership.disabled must leave it alone.
        rows = await repository.suspend_all_role_assignments_for_user(
            session, user_id=user_id, reason="user_disabled"
        )
        assert len(rows) == 2
        await repository.reactivate_role_assignments_suspended_for_reason(
            session,
            user_id=user_id,
            org_id=org_id,
            reason="user_disabled",
            role_id="catalog_manager",
        )

    envelope = _org_membership_envelope(
        event_type="membership.disabled",
        membership_id=membership_id,
        org_id=org_id,
        user_id=user_id,
        actor_id=str(uuid4()),
    )
    await _run(session_factory, event_handlers.handle_membership_disabled, envelope)

    async with session_factory() as session:
        rows = {
            r.role_id: r
            for r in await repository.list_role_assignments(session, domain=f"org:{org_id}")
        }
        assert rows["catalog_manager"].status == "suspended"
        assert rows["catalog_manager"].reason == "membership_disabled"
        # org_admin was already suspended for user_disabled and must stay that way.
        assert rows["org_admin"].status == "suspended"
        assert rows["org_admin"].reason == "user_disabled"


async def test_membership_reactivated_restores_only_membership_disabled_reason(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app import event_handlers, repository

    org_id, membership_id, user_id = uuid7(), uuid7(), uuid7()

    async with session_factory() as session, session.begin():
        await repository.grant_role_unconditional(
            session,
            user_id=user_id,
            role_id="order_manager",
            domain=f"org:{org_id}",
            org_id=org_id,
            membership_id=membership_id,
            granted_by=user_id,
            reason=None,
        )
        # Suspended for user_disabled (not membership_disabled) -- a later
        # membership.reactivated must not resurrect this row. Both suspend
        # functions only touch *active* rows, so this has to be the row's
        # one and only suspend to land it with this specific reason.
        await repository.suspend_all_role_assignments_for_user(
            session, user_id=user_id, reason="user_disabled"
        )

    envelope = _org_membership_envelope(
        event_type="membership.reactivated",
        membership_id=membership_id,
        org_id=org_id,
        user_id=user_id,
        actor_id=str(uuid4()),
    )
    await _run(session_factory, event_handlers.handle_membership_reactivated, envelope)

    async with session_factory() as session:
        rows = await repository.list_role_assignments(session, domain=f"org:{org_id}")
        # Suspended for "user_disabled" by the time membership.reactivated ran,
        # so reactivation (which only targets reason="membership_disabled") is a no-op.
        assert all(r.status == "suspended" for r in rows)


async def test_membership_revoked_revokes_assignments(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app import event_handlers, repository

    org_id, membership_id, user_id = uuid7(), uuid7(), uuid7()

    async with session_factory() as session, session.begin():
        await repository.grant_role_unconditional(
            session,
            user_id=user_id,
            role_id="catalog_manager",
            domain=f"org:{org_id}",
            org_id=org_id,
            membership_id=membership_id,
            granted_by=user_id,
            reason=None,
        )

    envelope = _org_membership_envelope(
        event_type="membership.revoked",
        membership_id=membership_id,
        org_id=org_id,
        user_id=user_id,
        actor_id=str(uuid4()),
    )
    await _run(session_factory, event_handlers.handle_membership_revoked, envelope)

    async with session_factory() as session:
        rows = await repository.list_role_assignments(session, domain=f"org:{org_id}")
        assert rows[0].status == "revoked"


async def test_organization_suspended_and_archived_write_domain_status_and_bump_version(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app import event_handlers
    from app.models import DomainStatus

    org_id = uuid7()
    envelope = build_envelope(
        event_type="organization.suspended",
        source="orgs-svc",
        subject=str(org_id),
        data={"org_id": str(org_id)},
        schema_version="1.0",
        aggregate_type="organization",
        aggregate_version=5,
    )

    version = await _run(session_factory, event_handlers.handle_organization_suspended, envelope)
    assert version is not None
    async with session_factory() as session:
        row = await session.get(DomainStatus, f"org:{org_id}")
        assert row is not None
        assert row.status == "suspended"
        assert row.source_version == 5


async def test_organization_status_out_of_order_is_a_no_op(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app import event_handlers
    from app.models import DomainStatus

    org_id = uuid7()
    newer = build_envelope(
        event_type="organization.archived",
        source="orgs-svc",
        subject=str(org_id),
        data={"org_id": str(org_id)},
        schema_version="1.0",
        aggregate_type="organization",
        aggregate_version=10,
    )
    stale = build_envelope(
        event_type="organization.suspended",
        source="orgs-svc",
        subject=str(org_id),
        data={"org_id": str(org_id)},
        schema_version="1.0",
        aggregate_type="organization",
        aggregate_version=3,  # older than what's already stored
    )

    await _run(session_factory, event_handlers.handle_organization_archived, newer)
    version = await _run(session_factory, event_handlers.handle_organization_suspended, stale)
    assert version is None  # stale event -> no-op, no policy_version bump

    async with session_factory() as session:
        row = await session.get(DomainStatus, f"org:{org_id}")
        assert row.status == "archived"  # unchanged by the stale event
        assert row.source_version == 10


async def test_organization_reactivated_also_bumps_the_version(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Deliberate deviation from LLD-02 §9's literal wording — see
    event_handlers.py's module docstring for why this must bump the version
    even though the table's row for this event omits "bump the version"."""
    from app import event_handlers

    org_id = uuid7()
    envelope = build_envelope(
        event_type="organization.reactivated",
        source="orgs-svc",
        subject=str(org_id),
        data={"org_id": str(org_id)},
        schema_version="1.0",
        aggregate_type="organization",
        aggregate_version=1,
    )

    version = await _run(session_factory, event_handlers.handle_organization_reactivated, envelope)
    assert version is not None


async def test_user_created_grants_customer_role(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app import event_handlers, repository

    user_id = uuid7()
    envelope = build_envelope(
        event_type="user.created",
        source="users-svc",
        subject=str(user_id),
        data={"user_id": str(user_id), "status": "active"},
        schema_version="1.0",
        aggregate_type="user",
        aggregate_version=1,
    )

    version = await _run(session_factory, event_handlers.handle_user_created, envelope)
    assert version is not None
    async with session_factory() as session:
        rows = await repository.list_role_assignments(session, domain="customer", user_id=user_id)
        assert len(rows) == 1
        assert rows[0].role_id == "customer"


async def test_user_disabled_suspends_every_domain(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app import event_handlers, repository

    user_id, org_id = uuid7(), uuid7()

    async with session_factory() as session, session.begin():
        await repository.grant_role_unconditional(
            session,
            user_id=user_id,
            role_id="customer",
            domain="customer",
            org_id=None,
            membership_id=None,
            granted_by=user_id,
            reason=None,
        )
        await repository.grant_role_unconditional(
            session,
            user_id=user_id,
            role_id="org_admin",
            domain=f"org:{org_id}",
            org_id=org_id,
            membership_id=None,
            granted_by=user_id,
            reason=None,
        )

    envelope = build_envelope(
        event_type="user.disabled",
        source="users-svc",
        subject=str(user_id),
        data={"user_id": str(user_id), "status": "disabled"},
        schema_version="1.0",
        aggregate_type="user",
        aggregate_version=2,
    )
    await _run(session_factory, event_handlers.handle_user_disabled, envelope)

    async with session_factory() as session:
        customer_rows = await repository.list_role_assignments(
            session, domain="customer", user_id=user_id
        )
        org_rows = await repository.list_role_assignments(
            session, domain=f"org:{org_id}", user_id=user_id
        )
        assert customer_rows[0].status == "suspended"
        assert customer_rows[0].reason == "user_disabled"
        assert org_rows[0].status == "suspended"
        assert org_rows[0].reason == "user_disabled"


async def test_user_reactivated_restores_only_customer_role(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app import event_handlers, repository

    user_id, org_id = uuid7(), uuid7()

    async with session_factory() as session, session.begin():
        await repository.grant_role_unconditional(
            session,
            user_id=user_id,
            role_id="customer",
            domain="customer",
            org_id=None,
            membership_id=None,
            granted_by=user_id,
            reason=None,
        )
        await repository.grant_role_unconditional(
            session,
            user_id=user_id,
            role_id="org_admin",
            domain=f"org:{org_id}",
            org_id=org_id,
            membership_id=None,
            granted_by=user_id,
            reason=None,
        )
        await repository.suspend_all_role_assignments_for_user(
            session, user_id=user_id, reason="user_disabled"
        )

    envelope = build_envelope(
        event_type="user.reactivated",
        source="users-svc",
        subject=str(user_id),
        data={"user_id": str(user_id), "status": "active"},
        schema_version="1.0",
        aggregate_type="user",
        aggregate_version=3,
    )
    version = await _run(session_factory, event_handlers.handle_user_reactivated, envelope)
    assert version is not None

    async with session_factory() as session:
        customer_rows = await repository.list_role_assignments(
            session, domain="customer", user_id=user_id
        )
        org_rows = await repository.list_role_assignments(
            session, domain=f"org:{org_id}", user_id=user_id
        )
        assert customer_rows[0].status == "active"
        # org_admin stays suspended -- only customer is restored by user.reactivated.
        assert org_rows[0].status == "suspended"
