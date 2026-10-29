"""Unit tests for retry-tier/DLQ routing (LLD-08 §7). No live RabbitMQ
needed -- the channel/exchange is faked, same "fake the external boundary"
style as sf-authz's own watcher tests.
"""

import time
from types import SimpleNamespace

import httpx
from sf_messaging.retry import (
    ATTEMPT_HEADER,
    FIRST_FAILED_HEADER,
    RETRY_TIERS,
    TransientError,
    is_transient,
    queue_name_for_tier,
    route_failure,
)
from sqlalchemy.exc import DBAPIError


class FakeExchange:
    def __init__(self) -> None:
        self.published: list[tuple[object, str]] = []

    async def publish(self, message: object, *, routing_key: str) -> None:
        self.published.append((message, routing_key))


class FakeChannel:
    def __init__(self) -> None:
        self.default_exchange = FakeExchange()


def _message(*, headers: dict | None = None, routing_key: str = "q.test") -> SimpleNamespace:
    acked = {"ack": False}

    async def ack() -> None:
        acked["ack"] = True

    return SimpleNamespace(
        body=b'{"id": "evt-1"}',
        headers=headers or {},
        content_type="application/json",
        message_id="evt-1",
        type="widget.created",
        routing_key=routing_key,
        ack=ack,
        _acked=acked,
    )


# --- is_transient -------------------------------------------------------------------------


def test_transient_error_is_transient() -> None:
    assert is_transient(TransientError("downstream said retry me")) is True


def test_httpx_connect_error_is_transient() -> None:
    assert is_transient(httpx.ConnectError("boom")) is True


def test_httpx_timeout_is_transient() -> None:
    assert is_transient(httpx.ReadTimeout("boom")) is True


def test_httpx_503_is_transient() -> None:
    request = httpx.Request("GET", "https://x.test")
    response = httpx.Response(503, request=request)
    assert is_transient(httpx.HTTPStatusError("x", request=request, response=response)) is True


def test_httpx_404_is_not_transient() -> None:
    request = httpx.Request("GET", "https://x.test")
    response = httpx.Response(404, request=request)
    assert is_transient(httpx.HTTPStatusError("x", request=request, response=response)) is False


def test_deadlock_sqlstate_is_transient() -> None:
    orig = SimpleNamespace(sqlstate="40P01")
    exc = DBAPIError("stmt", {}, Exception(), connection_invalidated=False)
    exc.orig = orig  # type: ignore[attr-defined]
    assert is_transient(exc) is True


def test_unrelated_db_error_is_not_transient() -> None:
    orig = SimpleNamespace(sqlstate="23505")  # unique_violation
    exc = DBAPIError("stmt", {}, Exception(), connection_invalidated=False)
    exc.orig = orig  # type: ignore[attr-defined]
    assert is_transient(exc) is False


def test_plain_runtime_error_is_not_transient() -> None:
    assert is_transient(RuntimeError("boom")) is False


# --- route_failure -------------------------------------------------------------------------


async def test_first_transient_failure_goes_to_retry_tier_1() -> None:
    channel = FakeChannel()
    message = _message(routing_key="q.widgets")

    result = await route_failure(
        channel,
        queue_name="q.widgets",
        message=message,
        consumer_name="c1",
        exc=TransientError("x"),
    )

    assert result == "retried"
    assert message._acked["ack"] is True
    (sent_message, routing_key) = channel.default_exchange.published[0]
    assert routing_key == queue_name_for_tier("q.widgets", 1)
    assert sent_message.headers[ATTEMPT_HEADER] == 1


async def test_attempt_header_advances_to_the_next_tier() -> None:
    channel = FakeChannel()
    message = _message(headers={ATTEMPT_HEADER: 2}, routing_key="q.widgets")

    result = await route_failure(
        channel,
        queue_name="q.widgets",
        message=message,
        consumer_name="c1",
        exc=TransientError("x"),
    )

    assert result == "retried"
    (_sent, routing_key) = channel.default_exchange.published[0]
    assert routing_key == queue_name_for_tier("q.widgets", 3)


async def test_exhausted_tiers_goes_to_the_dlq_instead() -> None:
    channel = FakeChannel()
    message = _message(headers={ATTEMPT_HEADER: len(RETRY_TIERS)}, routing_key="q.widgets")

    result = await route_failure(
        channel,
        queue_name="q.widgets",
        message=message,
        consumer_name="c1",
        exc=TransientError("x"),
    )

    assert result == "dead_lettered"
    (sent_message, routing_key) = channel.default_exchange.published[0]
    assert routing_key == "q.widgets.dlq"
    assert sent_message.headers["x-sf-error-class"] == "transient"


async def test_permanent_error_skips_retry_tiers_entirely() -> None:
    channel = FakeChannel()
    message = _message(routing_key="q.widgets")

    result = await route_failure(
        channel,
        queue_name="q.widgets",
        message=message,
        consumer_name="c1",
        exc=RuntimeError("boom"),
    )

    assert result == "dead_lettered"
    (sent_message, routing_key) = channel.default_exchange.published[0]
    assert routing_key == "q.widgets.dlq"
    assert sent_message.headers["x-sf-error-class"] == "permanent"
    assert "boom" in sent_message.headers["x-sf-error"]


async def test_dlq_metadata_headers_are_set() -> None:
    channel = FakeChannel()
    message = _message(headers={ATTEMPT_HEADER: len(RETRY_TIERS)}, routing_key="q.widgets")

    await route_failure(
        channel,
        queue_name="q.widgets",
        message=message,
        consumer_name="my-consumer",
        exc=RuntimeError("x"),
    )

    (sent_message, _rk) = channel.default_exchange.published[0]
    assert sent_message.headers["x-sf-consumer"] == "my-consumer"
    assert sent_message.headers["x-sf-attempts"] == len(RETRY_TIERS) + 1
    assert sent_message.headers["x-sf-original-routing-key"] == "q.widgets"
    assert FIRST_FAILED_HEADER in sent_message.headers


async def test_first_failed_at_is_preserved_across_retries() -> None:
    channel = FakeChannel()
    first_failed = str(time.time() - 100)
    message = _message(
        headers={ATTEMPT_HEADER: 1, FIRST_FAILED_HEADER: first_failed}, routing_key="q.widgets"
    )

    await route_failure(
        channel,
        queue_name="q.widgets",
        message=message,
        consumer_name="c1",
        exc=TransientError("x"),
    )

    (sent_message, _rk) = channel.default_exchange.published[0]
    assert sent_message.headers[FIRST_FAILED_HEADER] == first_failed
