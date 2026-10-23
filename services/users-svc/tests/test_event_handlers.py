"""``q.users.commands`` handlers (LLD-01 §11.2, §11.3) -- direct-call style.
The Keycloak call itself is faked: no real Keycloak admin API is involved,
only the handler's own dispatch logic."""

from types import SimpleNamespace
from uuid import UUID, uuid7

import pytest
from app import event_handlers
from sf_messaging import build_envelope


class _FakeKeycloakAdmin:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def set_sf_uid(self, *, keycloak_user_id: str, sf_uid: str) -> None:
        self.calls.append((keycloak_user_id, sf_uid))


def _envelope(*, user_id: str, idp_subject: str) -> dict:
    return build_envelope(
        event_type="user.idp_sync_requested",
        source="users-svc",
        subject=user_id,
        data={"user_id": user_id, "idp_subject": idp_subject},
        schema_version="1.0",
        aggregate_type="user",
        aggregate_version=1,
    )


class _FakeStatusAdmin:
    def __init__(self, *, found: bool = True) -> None:
        self.found = found
        self.calls: list[tuple[str, bool]] = []

    async def sync_enabled(self, *, keycloak_user_id: str, enabled: bool) -> bool:
        self.calls.append((keycloak_user_id, enabled))
        return self.found


def _status_envelope(user_id: str) -> dict:
    return build_envelope(
        event_type="user.idp_status_sync_requested",
        source="users-svc",
        subject=user_id,
        data={"user_id": user_id},
        schema_version="1.0",
        aggregate_type="user",
        aggregate_version=2,
    )


@pytest.mark.parametrize(("status", "enabled"), [("disabled", False), ("active", True)])
async def test_status_sync_applies_the_users_current_status(
    monkeypatch: pytest.MonkeyPatch, status: str, enabled: bool
) -> None:
    """The command carries no target state: the user row decides, so a
    disable/reactivate pair converges whichever command runs last."""
    user_id = uuid7()

    async def get_by_id(_session: object, uid: UUID) -> object:
        assert uid == user_id
        return SimpleNamespace(id=uid, idp_subject="kc-user-1", status=status)

    monkeypatch.setattr(event_handlers.repository, "get_by_id", get_by_id)
    fake = _FakeStatusAdmin()

    await event_handlers.build_dispatch(fake)["user.idp_status_sync_requested"](
        None, _status_envelope(str(user_id))
    )

    assert fake.calls == [("kc-user-1", enabled)]


async def test_status_sync_for_an_unknown_user_does_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def get_by_id(_session: object, _uid: UUID) -> None:
        return None

    monkeypatch.setattr(event_handlers.repository, "get_by_id", get_by_id)
    fake = _FakeStatusAdmin()

    await event_handlers.build_dispatch(fake)["user.idp_status_sync_requested"](
        None, _status_envelope(str(uuid7()))
    )

    assert fake.calls == []


async def test_retries_the_sf_uid_sync() -> None:
    user_id = str(uuid7())
    fake = _FakeKeycloakAdmin()
    dispatch = event_handlers.build_dispatch(fake)

    await dispatch["user.idp_sync_requested"](
        None, _envelope(user_id=user_id, idp_subject="kc-user-1")
    )

    assert fake.calls == [("kc-user-1", user_id)]
