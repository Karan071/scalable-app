"""Genuine end-to-end proof of the E2.8 chain: publish a real
``membership.created`` envelope onto the real ``q.authz.identity`` queue
against real local RabbitMQ, let a real ``InboxConsumer`` (wired to the
real ``event_handlers.DISPATCH`` table) process it against the real local
Postgres, then confirm via a real ``PolicyEnforcer.load()`` that the
granted user now passes ``enforce()`` — proving publish -> broker ->
consumer -> grant -> enforcer-visible without needing a second running
service (``FakeOrgsClient`` is used elsewhere in this test suite for the
same reason: avoid depending on a live sibling process).
"""

import asyncio
import json
import os
from collections.abc import AsyncIterator
from uuid import uuid4, uuid7

import aio_pika
import pytest
import pytest_asyncio
from sf_messaging import InboxConsumer, build_envelope
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_AMQP_URL = os.environ.get(
    "SF_TEST_AMQP_URL",
    f"amqp://{os.environ.get('SF_RABBITMQ_USER', 'scaleforge')}:"
    f"{os.environ.get('SF_RABBITMQ_PASSWORD', 'rabbitmq_local_dev')}@localhost:5672/"
    f"{os.environ['SF_RABBITMQ_VHOST']}",
)


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


async def test_membership_created_flows_through_the_real_queue_to_the_real_enforcer(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app import event_handlers
    from app.settings import Settings
    from sf_authz import PolicyEnforcer, build_enforcer

    settings = Settings()

    try:
        connection = await aio_pika.connect_robust(_AMQP_URL, timeout=3)
    except Exception as exc:
        pytest.skip(f"local RabbitMQ not reachable: {exc}")
        return

    org_id, membership_id, user_id, inviter = uuid7(), uuid7(), uuid7(), uuid7()
    envelope = build_envelope(
        event_type="membership.created",
        source="orgs-svc",
        subject=str(membership_id),
        data={
            "membership_id": str(membership_id),
            "org_id": str(org_id),
            "user_id": str(user_id),
            "roles_requested": ["org_admin"],
            "invited_by": str(inviter),
        },
        schema_version="1.0",
        aggregate_type="membership",
        aggregate_version=1,
    )

    consumer = InboxConsumer(
        consumer_name=f"test.{event_handlers.CONSUMER_NAME}.{uuid4().hex[:8]}",
        session_factory=session_factory,
        amqp_url=_AMQP_URL,
        queue_name="q.authz.identity",
        handlers=event_handlers.DISPATCH,
    )
    await consumer.start()
    try:
        channel = await connection.channel()
        exchange = await channel.declare_exchange(
            "sf.events", type="topic", durable=True, passive=True
        )
        await exchange.publish(
            aio_pika.Message(
                body=json.dumps(envelope).encode(),
                message_id=envelope["id"],
                type=envelope["type"],
            ),
            routing_key="membership.created.v1",
        )

        # Poll for *this specific* grant landing, not just "the consumer did
        # something" -- a shared queue could be mid-processing unrelated
        # backlog from elsewhere, so a generic "it handled something" signal
        # isn't proof this particular message was the one that landed.
        from app import repository

        granted = False
        for _ in range(50):
            async with session_factory() as session:
                rows = await repository.list_role_assignments(
                    session, domain=f"org:{org_id}", user_id=user_id
                )
            if rows:
                granted = True
                break
            await asyncio.sleep(0.1)
        assert granted, "consumer never processed the published message into a grant"
    finally:
        await consumer.stop()
        await connection.close()

    enforcer: PolicyEnforcer = build_enforcer(
        db_host=settings.db_host,
        db_port=settings.db_port,
        db_name=settings.db_name,
        db_user=settings.authz_reader_db_user,
        db_password=settings.authz_reader_db_password,
    )
    enforcer.load()
    subject = f"user:{user_id}"
    domain = f"org:{org_id}"

    assert enforcer.enforce(subject, domain, "org_settings", "read") is True
    assert enforcer.enforce(subject, domain, "member", "read") is True
    # Never granted anything in a different org -- proves tenant scoping held.
    assert enforcer.enforce(subject, f"org:{uuid4()}", "org_settings", "read") is False
