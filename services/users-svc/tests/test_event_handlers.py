"""``handle_identity_mfa_group_sync_requested`` against the real local
Postgres (skip if unreachable) -- same direct-call style as authz-svc's and
orgs-svc's test_event_handlers.py. The Keycloak call itself is faked: no
real Keycloak admin API is involved, only the handler's own lookup/dispatch
logic."""

from collections.abc import AsyncIterator
from uuid import uuid7

import pytest
import pytest_asyncio
from sf_messaging import build_envelope
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    try:
        from app.db import build_users_engine, build_users_session_factory
        from app.settings import Settings
    except Exception as exc:
        pytest.skip(f"could not import app modules: {exc}")
        return

    engine = build_users_engine(Settings())
    factory = build_users_session_factory(engine)
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


class _FakeKeycloakAdmin:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def add_user_to_group(self, *, keycloak_user_id: str, group_name: str) -> None:
        self.calls.append((keycloak_user_id, group_name))


async def test_mfa_group_sync_adds_the_users_keycloak_id_to_the_group(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app import event_handlers, repository

    async with session_factory() as session, session.begin():
        user = await repository.insert_user_if_absent(
            session,
            idp_issuer="https://kc.test/realms/scaleforge",
            idp_subject=f"kc-{uuid7()}",
            email=f"{uuid7()}@example.com",
            email_verified=True,
            display_name=None,
        )
        assert user is not None

    envelope = build_envelope(
        event_type="identity.mfa_group_sync_requested",
        source="authz-svc",
        subject=str(uuid7()),
        data={"user_id": str(user.id)},
        schema_version="1.0",
        aggregate_type="role_assignment",
        aggregate_version=1,
    )

    fake_admin = _FakeKeycloakAdmin()
    dispatch = event_handlers.build_dispatch(fake_admin)
    async with session_factory() as session, session.begin():
        await dispatch["identity.mfa_group_sync_requested"](session, envelope)

    assert fake_admin.calls == [(user.idp_subject, event_handlers.MFA_REQUIRED_GROUP)]


async def test_mfa_group_sync_is_a_no_op_for_an_unknown_user(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app import event_handlers

    envelope = build_envelope(
        event_type="identity.mfa_group_sync_requested",
        source="authz-svc",
        subject=str(uuid7()),
        data={"user_id": str(uuid7())},
        schema_version="1.0",
        aggregate_type="role_assignment",
        aggregate_version=1,
    )

    fake_admin = _FakeKeycloakAdmin()
    dispatch = event_handlers.build_dispatch(fake_admin)
    async with session_factory() as session, session.begin():
        await dispatch["identity.mfa_group_sync_requested"](session, envelope)

    assert fake_admin.calls == []
