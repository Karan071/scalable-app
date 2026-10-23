"""``handle_identity_mfa_group_sync_requested`` (``q.authz.commands``,
LLD-01 §9) -- direct-call style, same as test_event_handlers.py. The
Keycloak call itself is faked: no real Keycloak admin API is involved,
only the handler's own lookup/dispatch logic."""

from uuid import uuid7

from app import command_handlers
from sf_messaging import build_envelope


class _FakeKeycloakAdmin:
    def __init__(self, *, keycloak_id_by_sf_uid: dict[str, str] | None = None) -> None:
        self._keycloak_id_by_sf_uid = keycloak_id_by_sf_uid or {}
        self.group_calls: list[tuple[str, str]] = []

    async def find_keycloak_id_by_sf_uid(self, sf_uid: str) -> str | None:
        return self._keycloak_id_by_sf_uid.get(sf_uid)

    async def add_user_to_group(self, *, keycloak_user_id: str, group_name: str) -> None:
        self.group_calls.append((keycloak_user_id, group_name))


def _envelope(user_id: str) -> dict:
    return build_envelope(
        event_type="identity.mfa_group_sync_requested",
        source="authz-svc",
        subject=str(uuid7()),
        data={"user_id": user_id},
        schema_version="1.0",
        aggregate_type="role_assignment",
        aggregate_version=1,
    )


async def test_resolves_the_user_then_adds_them_to_the_mfa_group() -> None:
    user_id = str(uuid7())
    fake = _FakeKeycloakAdmin(keycloak_id_by_sf_uid={user_id: "kc-user-1"})
    dispatch = command_handlers.build_dispatch(fake)

    await dispatch["identity.mfa_group_sync_requested"](None, _envelope(user_id))

    assert fake.group_calls == [("kc-user-1", command_handlers.MFA_REQUIRED_GROUP)]


async def test_is_a_no_op_when_keycloak_has_no_matching_user() -> None:
    fake = _FakeKeycloakAdmin()
    dispatch = command_handlers.build_dispatch(fake)

    await dispatch["identity.mfa_group_sync_requested"](None, _envelope(str(uuid7())))

    assert fake.group_calls == []
