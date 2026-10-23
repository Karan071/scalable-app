"""``handle_user_disabled`` against the real local Postgres (skip if
unreachable) -- same direct-call style as authz-svc's test_event_handlers.py."""

from collections.abc import AsyncIterator
from uuid import UUID, uuid7

import pytest
import pytest_asyncio
from sf_messaging import build_envelope
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    try:
        from app.db import build_orgs_engine, build_orgs_session_factory
        from app.settings import Settings
    except Exception as exc:
        pytest.skip(f"could not import app modules: {exc}")
        return

    engine = build_orgs_engine(Settings())
    factory = build_orgs_session_factory(engine)
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


async def _run(session_factory, handler, envelope: dict) -> None:
    async with session_factory() as session, session.begin():
        await handler(session, envelope)


async def _insert_active_membership(
    session_factory, *, org_id: UUID, user_id: UUID
) -> UUID:
    from app import repository

    async with session_factory() as session, session.begin():
        membership = await repository.upsert_active_membership(
            session, org_id=org_id, user_id=user_id, invited_by=None
        )
        return membership.id


async def test_user_disabled_disables_every_active_membership(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app import event_handlers, repository

    user_id, org_a, org_b = uuid7(), uuid7(), uuid7()
    membership_a = await _insert_active_membership(session_factory, org_id=org_a, user_id=user_id)
    membership_b = await _insert_active_membership(session_factory, org_id=org_b, user_id=user_id)

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
        rows = await repository.list_memberships_for_user(session, user_id)
        by_id = {row.id: row for row in rows}
        assert by_id[membership_a].status == "disabled"
        assert by_id[membership_a].status_reason == "user_disabled"
        assert by_id[membership_b].status == "disabled"
        assert by_id[membership_b].status_reason == "user_disabled"

        emitted = (
            (
                await session.execute(
                    text(
                        "SELECT payload FROM outbox "
                        "WHERE payload->>'type' = 'membership.disabled' "
                        "AND payload->'data'->>'user_id' = :user_id"
                    ),
                    {"user_id": str(user_id)},
                )
            )
            .mappings()
            .all()
        )
        assert {row["payload"]["data"]["org_id"] for row in emitted} == {
            str(org_a),
            str(org_b),
        }


async def test_user_disabled_leaves_already_disabled_membership_alone(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A membership the admin already disabled manually must not be
    re-emitted as a duplicate ``membership.disabled`` -- the handler only
    touches rows still in status ``active``."""
    from app import event_handlers, repository, service

    user_id, org_id = uuid7(), uuid7()
    membership_id = await _insert_active_membership(session_factory, org_id=org_id, user_id=user_id)

    async with session_factory() as session, session.begin():
        await service.disable_membership(
            session,
            membership_id,
            org_id=org_id,
            actor_id=uuid7(),
            reason="manual",
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
        rows = await repository.list_memberships_for_user(session, user_id)
        assert rows[0].status_reason == "manual"  # untouched by the handler

        emitted = (
            (
                await session.execute(
                    text(
                        "SELECT payload FROM outbox "
                        "WHERE payload->>'type' = 'membership.disabled' "
                        "AND payload->'data'->>'membership_id' = :membership_id"
                    ),
                    {"membership_id": str(membership_id)},
                )
            )
            .mappings()
            .all()
        )
        assert len(emitted) == 1  # only the manual disable's own event
