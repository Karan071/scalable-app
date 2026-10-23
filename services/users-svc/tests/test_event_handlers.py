"""``handle_user_idp_sync_requested`` (``q.users.commands``, LLD-01 §11.2)
-- direct-call style. The Keycloak call itself is faked: no real Keycloak
admin API is involved, only the handler's own dispatch logic."""

from uuid import uuid7

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


async def test_retries_the_sf_uid_sync() -> None:
    user_id = str(uuid7())
    fake = _FakeKeycloakAdmin()
    dispatch = event_handlers.build_dispatch(fake)

    await dispatch["user.idp_sync_requested"](
        None, _envelope(user_id=user_id, idp_subject="kc-user-1")
    )

    assert fake.calls == [("kc-user-1", user_id)]
