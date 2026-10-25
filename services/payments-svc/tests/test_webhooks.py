"""Integration tests against the local Compose Postgres (`make test-env`).
Skip if unreachable. LLD-06 §8.2: signature verification, dedup, the
metadata-correlation race (an "unknown" attempt with no
``provider_intent_id`` yet), and the out-of-order sequence guard.
"""

import hashlib
import hmac
import json
import os
import time
from collections.abc import AsyncIterator
from uuid import uuid4, uuid7

import pytest
import pytest_asyncio
from app import service
from app.models import PaymentAttempt
from fakes import FakeProviderClient
from sf_db import build_session_factory
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

pytestmark = pytest.mark.asyncio(loop_scope="module")

_SECRET = "test-webhook-secret"


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def engine() -> AsyncIterator[AsyncEngine]:
    try:
        eng = create_async_engine(
            f"postgresql+asyncpg://svc_payments:{os.environ['SF_DB_PASSWORD']}"
            f"@{os.environ['SF_DB_HOST']}:{os.environ['SF_DB_PORT']}/{os.environ['SF_DB_NAME']}"
        )
        async with eng.connect():
            pass
    except Exception as exc:
        pytest.skip(f"local Postgres not reachable: {exc}")
    yield eng
    await eng.dispose()


@pytest.fixture
def session_factory(engine: AsyncEngine) -> async_sessionmaker:
    return build_session_factory(engine)


def _sign(body: bytes, *, secret: str = _SECRET, timestamp: int | None = None) -> str:
    t = timestamp if timestamp is not None else int(time.time())
    signature = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={t},v1={signature}"


async def test_verify_signature_accepts_a_valid_signature() -> None:
    body = b'{"hello": "world"}'
    header = _sign(body)

    service.verify_signature(
        secrets=[_SECRET], signature_header=header, body=body, clock_skew_tolerance_s=300
    )


async def test_verify_signature_rejects_a_bad_signature() -> None:
    body = b'{"hello": "world"}'
    header = _sign(body, secret="wrong-secret")

    with pytest.raises(service.SignatureInvalid):
        service.verify_signature(
            secrets=[_SECRET], signature_header=header, body=body, clock_skew_tolerance_s=300
        )


async def test_verify_signature_rejects_a_stale_timestamp() -> None:
    body = b'{"hello": "world"}'
    header = _sign(body, timestamp=int(time.time()) - 600)

    with pytest.raises(service.TimestampStale):
        service.verify_signature(
            secrets=[_SECRET], signature_header=header, body=body, clock_skew_tolerance_s=300
        )


def _event(
    *,
    provider_event_id: str,
    event_type: str,
    intent_id: str,
    sequence: int,
    metadata: dict | None = None,
) -> dict:
    return {
        "provider_event_id": provider_event_id,
        "type": event_type,
        "data": {
            "object": {
                "id": intent_id,
                "status": "authorized",
                "amount_minor": 1500,
                "currency": "EUR",
                "sequence": sequence,
                "metadata": metadata or {},
            }
        },
    }


async def test_process_webhook_dedups_a_redelivered_event(
    session_factory: async_sessionmaker,
) -> None:
    org_id, order_id, customer_id = uuid4(), uuid7(), uuid7()
    fake = FakeProviderClient()
    outcome = await service.authorize(
        session_factory,
        fake,
        order_id=order_id,
        org_id=org_id,
        customer_id=customer_id,
        amount_minor=1500,
        currency="EUR",
        payment_method_token="pm_mock_success",
    )
    async with session_factory() as session:
        attempt = await session.get(PaymentAttempt, outcome.payment_id)
    assert attempt is not None and attempt.provider_intent_id is not None

    event = _event(
        provider_event_id=f"evt_dup_{uuid7()}",
        event_type="payment_intent.captured",
        intent_id=attempt.provider_intent_id,
        sequence=2,
    )
    body = json.dumps(event).encode()

    first = await service.process_webhook(
        session_factory, event=event, payload_sha256=hashlib.sha256(body).hexdigest()
    )
    second = await service.process_webhook(
        session_factory, event=event, payload_sha256=hashlib.sha256(body).hexdigest()
    )

    assert first == "processed"
    assert second == "duplicate"


async def test_process_webhook_correlates_via_metadata_for_an_unknown_attempt(
    session_factory: async_sessionmaker,
) -> None:
    fake = FakeProviderClient()
    fake.force_authorize_timeout("pm_mock_timeout")
    org_id, order_id, customer_id = uuid4(), uuid7(), uuid7()
    outcome = await service.authorize(
        session_factory,
        fake,
        order_id=order_id,
        org_id=org_id,
        customer_id=customer_id,
        amount_minor=1500,
        currency="EUR",
        payment_method_token="pm_mock_timeout",
    )
    assert outcome.status == "unknown"

    # The provider's webhook still carries our own attempt_id in metadata,
    # even though the synchronous response never gave us its intent id.
    # A fresh intent id per run: a hardcoded one would collide with
    # whatever a previous run against this same persistent test DB
    # already correlated it to.
    intent_id = f"provider_intent_{uuid7()}"
    event = _event(
        provider_event_id=f"evt_correlate_{uuid7()}",
        event_type="payment_intent.authorized",
        intent_id=intent_id,
        sequence=1,
        metadata={"attempt_id": str(outcome.payment_id)},
    )

    result = await service.process_webhook(
        session_factory, event=event, payload_sha256="irrelevant"
    )

    assert result == "processed"
    async with session_factory() as session:
        attempt = await session.get(PaymentAttempt, outcome.payment_id)
    assert attempt is not None
    assert attempt.status == "authorized"
    assert attempt.provider_intent_id == intent_id


async def test_process_webhook_ignores_a_stale_out_of_order_event(
    session_factory: async_sessionmaker,
) -> None:
    fake = FakeProviderClient()
    org_id, order_id, customer_id = uuid4(), uuid7(), uuid7()
    outcome = await service.authorize(
        session_factory,
        fake,
        order_id=order_id,
        org_id=org_id,
        customer_id=customer_id,
        amount_minor=1500,
        currency="EUR",
        payment_method_token="pm_mock_success",
    )
    async with session_factory() as session:
        attempt = await session.get(PaymentAttempt, outcome.payment_id)
    assert attempt is not None and attempt.provider_intent_id is not None
    intent_id = attempt.provider_intent_id

    # "captured" (sequence 2) arrives first -- out of order relative to
    # authorization, but still the higher sequence.
    captured_event = _event(
        provider_event_id=f"evt_ooo_captured_{uuid7()}",
        event_type="payment_intent.captured",
        intent_id=intent_id,
        sequence=2,
    )
    await service.process_webhook(session_factory, event=captured_event, payload_sha256="x")

    # The (logically earlier) "authorized" webhook, sequence 1, arrives
    # after -- must be ignored as stale, not regress the status.
    authorized_event = _event(
        provider_event_id=f"evt_ooo_authorized_{uuid7()}",
        event_type="payment_intent.authorized",
        intent_id=intent_id,
        sequence=1,
    )
    result = await service.process_webhook(
        session_factory, event=authorized_event, payload_sha256="y"
    )

    assert result == "ignored"
    async with session_factory() as session:
        refreshed = await session.get(PaymentAttempt, outcome.payment_id)
    assert refreshed is not None
    assert refreshed.status == "captured"  # unchanged by the stale event
