"""Unit tests for proxy.py's refresh-lock and step-up translation (LLD-01 §6, §9).

No live Redis/Keycloak/httpx needed — the external boundaries (redis client,
OidcClient, SessionStore) are faked, same style as sf-authz's
test_watcher.py fake pubsub/redis doubles.
"""

import time
from types import SimpleNamespace

import httpx
import pytest
from app.proxy import ConcurrencyGate, ForwardResult, ensure_fresh_access_token, translate_step_up
from app.sessions import SessionRecord, key_hash
from fastapi import HTTPException


def _record(*, at_exp: int) -> SessionRecord:
    return SessionRecord(
        uid="u1",
        sub="sub1",
        idp_sid="sid1",
        access_token="old-at",
        refresh_token="old-rt",
        id_token="idt",
        at_exp=at_exp,
        acr="1",
        auth_time=int(time.time()),
        csrf="csrf-secret",
        created=int(time.time()),
        last_seen=int(time.time()),
        abs_exp=int(time.time()) + 10_000,
        session_class="standard",
    )


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def set(self, key: str, value: str, *, nx: bool = False, px: int | None = None) -> bool:
        if nx and key in self.store:
            return False
        self.store[key] = value
        return True

    async def eval(self, _script: str, _numkeys: int, key: str, token: str) -> int:
        if self.store.get(key) == token:
            del self.store[key]
            return 1
        return 0


class FakeOidc:
    def __init__(self, *, response: dict | None = None, error: Exception | None = None) -> None:
        self._response = response
        self._error = error
        self.calls = 0

    async def refresh(self, refresh_token: str) -> dict[str, object]:
        self.calls += 1
        if self._error is not None:
            raise self._error
        assert self._response is not None
        return self._response


class FakeSessions:
    def __init__(self) -> None:
        self.updated: dict | None = None
        self.deleted: str | None = None
        self.peek_queue: list[tuple[str, int] | None] = []

    async def update_tokens(
        self, session_id: str, *, access_token: str, refresh_token: str, at_exp: int
    ) -> None:
        self.updated = {
            "session_id": session_id,
            "access_token": access_token,
            "refresh_token": refresh_token,
            "at_exp": at_exp,
        }

    async def delete(self, session_id: str) -> None:
        self.deleted = session_id

    async def peek_access_token(self, session_id: str) -> tuple[str, int] | None:
        return self.peek_queue.pop(0) if self.peek_queue else None


def _state(*, redis: FakeRedis, oidc: FakeOidc, sessions: FakeSessions) -> SimpleNamespace:
    return SimpleNamespace(redis=redis, oidc=oidc, sessions=sessions)


async def test_ensure_fresh_access_token_returns_current_token_when_not_near_expiry() -> None:
    record = _record(at_exp=int(time.time()) + 3600)
    state = _state(redis=FakeRedis(), oidc=FakeOidc(), sessions=FakeSessions())

    token = await ensure_fresh_access_token(state, "sess-1", record)

    assert token == "old-at"


async def test_ensure_fresh_access_token_refreshes_when_near_expiry() -> None:
    record = _record(at_exp=int(time.time()) + 10)
    oidc = FakeOidc(
        response={"access_token": "new-at", "refresh_token": "new-rt", "expires_in": 300}
    )
    sessions = FakeSessions()
    state = _state(redis=FakeRedis(), oidc=oidc, sessions=sessions)

    token = await ensure_fresh_access_token(state, "sess-1", record)

    assert token == "new-at"
    assert sessions.updated is not None
    assert sessions.updated["access_token"] == "new-at"
    assert oidc.calls == 1


async def test_ensure_fresh_access_token_invalid_grant_deletes_session_and_raises_401() -> None:
    record = _record(at_exp=int(time.time()) + 10)
    response = httpx.Response(
        400, json={"error": "invalid_grant"}, request=httpx.Request("POST", "https://kc.test")
    )
    error = httpx.HTTPStatusError("bad", request=response.request, response=response)
    sessions = FakeSessions()
    state = _state(redis=FakeRedis(), oidc=FakeOidc(error=error), sessions=sessions)

    with pytest.raises(HTTPException) as exc_info:
        await ensure_fresh_access_token(state, "sess-1", record)

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "session_expired"
    assert sessions.deleted == "sess-1"


async def test_ensure_fresh_access_token_transient_failure_falls_back_to_current() -> None:
    """at_exp still >=5s out, so a transient 5xx/timeout doesn't fail the request."""
    record = _record(at_exp=int(time.time()) + 10)
    response = httpx.Response(503, request=httpx.Request("POST", "https://kc.test"))
    error = httpx.HTTPStatusError("down", request=response.request, response=response)
    state = _state(redis=FakeRedis(), oidc=FakeOidc(error=error), sessions=FakeSessions())

    token = await ensure_fresh_access_token(state, "sess-1", record)

    assert token == "old-at"


async def test_ensure_fresh_access_token_lock_loser_polls_and_picks_up_winners_refresh() -> None:
    record = _record(at_exp=int(time.time()) + 10)
    redis = FakeRedis()
    # Pre-seed the lock as held by someone else.
    redis.store[f"sess:lock:{key_hash('sess-1')}"] = "someone-else"
    sessions = FakeSessions()
    # First poll: the winner hasn't written yet (unchanged at_exp). Second: it has.
    sessions.peek_queue = [("old-at", record.at_exp), ("refreshed-at", record.at_exp + 300)]
    state = _state(redis=redis, oidc=FakeOidc(), sessions=sessions)

    token = await ensure_fresh_access_token(state, "sess-1", record)

    assert token == "refreshed-at"


def test_translate_step_up_rewrites_a_downstream_401() -> None:
    body = b'{"type":"about:blank","title":"step_up_required","status":401}'
    result = ForwardResult(status_code=401, headers=httpx.Headers({}), body=body)

    response = translate_step_up(result)

    assert response is not None
    assert response.status_code == 401
    import json

    content = json.loads(response.body)
    assert content["title"] == "step_up_required"
    assert content["step_up_url"] == "/auth/step-up"


def test_translate_step_up_passes_through_other_401s() -> None:
    body = b'{"type":"about:blank","title":"unauthenticated","status":401}'
    result = ForwardResult(status_code=401, headers=httpx.Headers({}), body=body)

    assert translate_step_up(result) is None


def test_translate_step_up_passes_through_non_401s() -> None:
    result = ForwardResult(status_code=200, headers=httpx.Headers({}), body=b"{}")

    assert translate_step_up(result) is None


async def test_concurrency_gate_rejects_once_full() -> None:
    gate = ConcurrencyGate(max_concurrency=1)

    assert await gate.try_acquire() is True
    assert await gate.try_acquire() is False

    await gate.release()
    assert await gate.try_acquire() is True
