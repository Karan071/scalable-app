"""M4 exit evidence: real-time order-status SSE end to end (LLD-06 §10).

    make test-system

Real RabbitMQ, real redis-cache/redis-session, the real Compose Keycloak,
one real BFF process. No other services: SSE only needs the event to
actually be on the wire, not a real checkout to produce it -- published
directly here, same style as test_bulkheads.py's own
``_publish_mfa_sync_commands``. The session is seeded directly into Redis
(this file's own ``_seed_session``) rather than through a real login --
test_bff_login.py already covers that path end to end; this file's only
concern is the SSE pipeline itself, and importing bff's own
``SessionStore`` isn't an option here (every service's wheel installs a
top-level package literally named ``app`` -- harness.py's own module
docstring has the "why" this file can't import one service's ``app.*``
from within tests/system).

Real Keycloak, not harness.py's ``FakeIdP``: the BFF's own startup calls
``discover()`` against ``{issuer}/.well-known/openid-configuration``, and
``FakeIdP.issuer`` is a fixed unreachable placeholder string
(``http://fake-idp.test/...``) used only as a JWT `iss` claim elsewhere --
every other ``FakeIdP`` consumer is a backend service that never needs a
real discovery fetch (confirmed by this file's own first version, which
tried `FakeIdP` and failed BFF startup with a DNS error). No login
actually happens here, so Keycloak's own redirect-URI registration (the
E3.8/18180 fixture) doesn't matter -- this never reaches `/auth/login`.
"""

import hashlib
import json
import time
from collections.abc import AsyncIterator
from uuid import uuid7

import aio_pika
import httpx
import pytest
import pytest_asyncio
from harness import ENV, TEST_VHOST, ServiceProcess, service_env
from keycloak import KC, KeycloakIdP
from redis.asyncio import Redis
from sf_common import TokenCipher
from sf_messaging import build_envelope

pytestmark = pytest.mark.asyncio(loop_scope="module")

_BFF_PORT = 18190
_SESSION_REDIS_URL = "redis://localhost:6379/3"
_CACHE_REDIS_URL = "redis://localhost:6380/3"
_SESSION_KEYS = {"k1": "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="}
_CIPHER = TokenCipher.from_base64_keys(_SESSION_KEYS, "k1")


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def bff() -> AsyncIterator[ServiceProcess]:
    async with httpx.AsyncClient(timeout=10) as http:
        try:
            (await http.get(f"{KC}/realms/scaleforge")).raise_for_status()
        except httpx.HTTPError as exc:
            pytest.skip(f"Compose Keycloak not reachable: {exc}")

    idp = KeycloakIdP()
    process = ServiceProcess(
        "bff",
        port=_BFF_PORT,
        label="sse-bff",
        env=service_env(
            "bff",
            idp,
            SF_OIDC_CLIENT_ID="sf-bff",
            SF_OIDC_CLIENT_SECRET=ENV["SF_BFF_CLIENT_SECRET"],
            # Never actually visited -- this file never drives a login --
            # so it doesn't need to be a registered redirect URI.
            SF_OIDC_REDIRECT_URI=f"http://localhost:{_BFF_PORT}/auth/callback",
            SF_APP_ORIGIN=f"http://localhost:{_BFF_PORT}",
            SF_COOKIE_SECURE="false",
            SF_SVC_BFF_CLIENT_SECRET=ENV["SF_SVC_BFF_CLIENT_SECRET"],
            SF_USERS_SVC_BASE_URL="http://127.0.0.1:1",
            SF_ORGS_SVC_BASE_URL="http://127.0.0.1:1",
            SF_AUTHZ_SVC_BASE_URL="http://127.0.0.1:1",
            SF_CATALOG_SVC_BASE_URL="http://127.0.0.1:1",
            SF_SEARCH_SVC_BASE_URL="http://127.0.0.1:1",
            SF_CART_SVC_BASE_URL="http://127.0.0.1:1",
            SF_INVENTORY_SVC_BASE_URL="http://127.0.0.1:1",
            SF_ORDERS_SVC_BASE_URL="http://127.0.0.1:1",
            SF_REDIS_SESSION_URL=_SESSION_REDIS_URL,
            SF_REDIS_CACHE_URL=_CACHE_REDIS_URL,
            SF_SESSION_ENC_KEYS=json.dumps(_SESSION_KEYS),
            SF_SESSION_ENC_PRIMARY_KID="k1",
        ),
    )
    await process.start()
    try:
        yield process
    finally:
        process.stop()


async def _seed_session(uid: str) -> str:
    """Replicates ``app.sessions.SessionStore.create()``'s own Redis
    layout directly (module docstring: can't import it from here)."""
    session_id = "sse-system-test-session-id"
    h = hashlib.sha256(session_id.encode()).hexdigest()
    aad = h.encode()
    now = int(time.time())
    fields = {
        "uid": uid,
        "sub": f"sub-{uid}",
        "idp_sid": f"idp-{uid}",
        "at": _CIPHER.encrypt("at", aad=aad),
        "rt": _CIPHER.encrypt("rt", aad=aad),
        "idt": _CIPHER.encrypt("idt", aad=aad),
        "at_exp": now + 300,
        "acr": "1",
        "auth_time": now,
        "csrf": "csrf-secret",
        "created": now,
        "last_seen": now,
        "abs_exp": now + 43200,
        "class": "standard",
        "v": 1,
    }
    redis: Redis = Redis.from_url(_SESSION_REDIS_URL, decode_responses=True)
    try:
        await redis.hset(f"sess:{h}", mapping=fields)  # type: ignore[arg-type]
        await redis.expire(f"sess:{h}", 1800)
    finally:
        await redis.aclose()
    return session_id


async def _publish_order_status_changed(*, order_id: str, customer_id: str, to: str) -> None:
    url = (
        f"amqp://{ENV['SF_RABBITMQ_USER']}:{ENV['SF_RABBITMQ_PASSWORD']}"
        f"@localhost:5672/{TEST_VHOST}"
    )
    connection = await aio_pika.connect_robust(url)
    try:
        channel = await connection.channel()
        exchange = await channel.get_exchange("sf.events")
        envelope = build_envelope(
            event_type="order.status_changed",
            source="orders-svc",
            subject=order_id,
            data={
                "order_id": order_id,
                "customer_id": customer_id,
                "org_id": str(uuid7()),
                "from": "pending",
                "to": to,
            },
            schema_version="1.0",
            aggregate_type="order",
            aggregate_version=1,
        )
        await exchange.publish(
            aio_pika.Message(json.dumps(envelope).encode(), message_id=envelope["id"]),
            routing_key="order.status_changed.v1",
        )
    finally:
        await connection.close()


async def test_order_status_changed_reaches_the_open_sse_stream(bff: ServiceProcess) -> None:
    customer_id = str(uuid7())
    order_id = str(uuid7())
    session_id = await _seed_session(customer_id)

    async with httpx.AsyncClient(timeout=10.0) as http:
        async with http.stream(
            "GET",
            f"{bff.base_url}/api/events",
            headers={"cookie": f"sf_session={session_id}"},
        ) as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")

            await _publish_order_status_changed(
                order_id=order_id, customer_id=customer_id, to="confirmed"
            )

            lines: list[str] = []
            async for line in resp.aiter_lines():
                if line:
                    lines.append(line)
                if any(line.startswith("data:") for line in lines):
                    break

    data_line = next(line for line in lines if line.startswith("data:"))
    payload = json.loads(data_line.removeprefix("data:").strip())
    assert payload["order_id"] == order_id
    assert payload["to"] == "confirmed"
