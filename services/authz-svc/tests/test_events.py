"""``emit_mfa_group_sync`` must publish to ``sf.commands``, not ``sf.events``
(LLD-08 §4.2: this is a command, not a fact) -- regression test for the bug
where it inherited ``emit()``'s event default, making the outbox row
permanently unroutable (no ``sf.events`` binding matches this routing key)."""

from collections.abc import AsyncIterator
from uuid import uuid7

import pytest
import pytest_asyncio
from sqlalchemy import select, text
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


async def test_emit_mfa_group_sync_uses_the_commands_exchange(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app import repository
    from app.events import emit_mfa_group_sync

    user_id, org_id = uuid7(), uuid7()

    async with session_factory() as session, session.begin():
        assignment = await repository.insert_role_assignment(
            session,
            user_id=user_id,
            role_id="org_admin",
            domain=f"org:{org_id}",
            org_id=org_id,
            membership_id=None,
            granted_by=user_id,
            reason=None,
        )
        await emit_mfa_group_sync(session, assignment, required=True)

        row = (
            (
                await session.execute(
                    text(
                        "SELECT exchange, routing_key, payload FROM outbox "
                        "WHERE aggregate_id = :aggregate_id "
                        "AND payload->>'type' = 'identity.mfa_group_sync_requested'"
                    ),
                    {"aggregate_id": str(assignment.id)},
                )
            )
            .mappings()
            .one()
        )

    assert row["exchange"] == "sf.commands"
    assert row["routing_key"] == "identity.mfa_group_sync_requested.v1"
    assert row["payload"]["data"]["user_id"] == str(user_id)
    assert row["payload"]["data"]["required"] is True
