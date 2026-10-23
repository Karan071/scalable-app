"""LLD-09 §2 bulkheads: a background backlog must not starve request handling.

    make test-system

authz-svc's command consumer calls Keycloak's admin API. Here Keycloak's
token endpoint answers in 5 s (LLD-13 FS-3, slow dependency) while 40
commands are queued, and a cheap DB-backed request must still answer fast.
"""

import asyncio
import json
import time
from collections.abc import AsyncIterator
from uuid import uuid7

import aio_pika
import httpx
import pytest_asyncio
from harness import ENV, TEST_VHOST, FakeIdP, ServiceProcess, service_env
from sf_messaging import DEFAULT_HANDLER_CONCURRENCY, build_envelope

AUTHZ_PORT = 18122


@pytest_asyncio.fixture
async def authz_with_slow_keycloak() -> AsyncIterator[tuple[ServiceProcess, FakeIdP]]:
    idp = FakeIdP(token_delay_s=5.0)
    authz = ServiceProcess(
        "authz-svc",
        port=AUTHZ_PORT,
        label="authz-svc-bulkheads",
        env=service_env("authz-svc", idp, SF_ORGS_SVC_BASE_URL="http://127.0.0.1:1"),
    )
    await authz.start()
    try:
        yield authz, idp
    finally:
        authz.stop()
        idp.stop()


async def _publish_mfa_sync_commands(n: int) -> None:
    url = (
        f"amqp://{ENV['SF_RABBITMQ_USER']}:{ENV['SF_RABBITMQ_PASSWORD']}"
        f"@localhost:5672/{TEST_VHOST}"
    )
    connection = await aio_pika.connect_robust(url)
    try:
        channel = await connection.channel()
        exchange = await channel.get_exchange("sf.commands")
        for _ in range(n):
            envelope = build_envelope(
                event_type="identity.mfa_group_sync_requested",
                source="authz-svc",
                subject=str(uuid7()),
                data={"user_id": str(uuid7()), "required": True},
                schema_version="1.0",
                aggregate_type="role_assignment",
                aggregate_version=1,
            )
            await exchange.publish(
                aio_pika.Message(json.dumps(envelope).encode(), message_id=envelope["id"]),
                routing_key="identity.mfa_group_sync_requested.v1",
            )
    finally:
        await connection.close()


async def test_a_slow_dependency_backlog_cannot_starve_requests(
    authz_with_slow_keycloak: tuple[ServiceProcess, FakeIdP],
) -> None:
    authz, idp = authz_with_slow_keycloak
    user = uuid7()
    headers = {"authorization": f"Bearer {idp.user_token(user, email=f'{user}@example.com')}"}

    await _publish_mfa_sync_commands(40)
    await asyncio.sleep(1.0)  # handlers are now in flight, waiting on Keycloak

    # Precondition, so a pass can't be vacuous: handlers are stuck on Keycloak
    # while the requests below run.
    assert idp.tokens_in_flight > 0

    results = []
    async with httpx.AsyncClient(timeout=10.0) as client:
        for _ in range(5):
            started = time.monotonic()
            resp = await client.get(f"{authz.base_url}/v1/roles", headers=headers)
            results.append((resp.status_code, round(time.monotonic() - started, 3)))

    assert idp.tokens_in_flight > 0
    assert all(status == 200 for status, _ in results), results
    assert all(seconds < 1.0 for _, seconds in results), results
    # LLD-08 §6: at most 10 handlers per consumer at once, not prefetch (20).
    assert idp.max_tokens_in_flight <= DEFAULT_HANDLER_CONCURRENCY
