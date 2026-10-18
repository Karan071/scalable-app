"""Shared test helpers: fake orgs-svc client + Principal overrides.

Route tests override the ``Authorize(...)``/``AuthenticatedUser()`` dependency
objects directly (FastAPI keys overrides by the exact callable passed to
``Depends``) rather than minting real signed JWTs — the HTTP-layer gate
itself (JWT validation, acr/staleness checks, the Casbin ``enforce()`` call)
is already covered by sf-authz's own dedicated tests. These tests exercise
the business logic and the *real* Casbin enforcement called from inside
``service.py`` (escalation guards), against the real local Postgres.
"""

from dataclasses import dataclass
from uuid import UUID

from app.orgs_client import MembershipStatus
from sf_authz import Principal


@dataclass
class FakeOrgsClient:
    """Stand-in for ``OrgsClient`` — no real orgs-svc process is running
    (no Compose app profile exists yet for any service). Each membership_id
    maps to a specific (user_id, status); unknown membership_ids default to
    ``default_user_id``/``default_status``."""

    default_user_id: UUID
    default_status: str = "active"
    overrides: dict[str, tuple[UUID, str]] | None = None

    def __post_init__(self) -> None:
        if self.overrides is None:
            self.overrides = {}

    async def get_membership(self, membership_id: str, *, org_id: str) -> MembershipStatus | None:
        user_id, status = self.overrides.get(  # type: ignore[union-attr]
            membership_id, (self.default_user_id, self.default_status)
        )
        if status == "missing":
            return None
        return MembershipStatus(
            id=membership_id, org_id=org_id, user_id=str(user_id), status=status, version=1
        )


def user_principal(sf_uid: UUID, *, acr: int = 0, auth_time: float | None = None) -> Principal:
    claims: dict[str, object] = {"sf_uid": str(sf_uid), "acr": acr}
    if auth_time is not None:
        claims["auth_time"] = auth_time
    return Principal(subject=f"user:{sf_uid}", audiences=("scaleforge-api",), claims=claims)


async def grant_role_directly(
    *, user_id: UUID, role_id: str, domain: str, org_id: UUID | None, granted_by: UUID
) -> None:
    """Bypasses the API to set up test fixtures (no orgs-svc process is
    running to drive a real assignment through)."""
    from app import repository
    from app.db import build_authz_engine, build_authz_session_factory
    from app.settings import Settings

    engine = build_authz_engine(Settings())
    factory = build_authz_session_factory(engine)
    async with factory() as session, session.begin():
        await repository.insert_role_assignment(
            session,
            user_id=user_id,
            role_id=role_id,
            domain=domain,
            org_id=org_id,
            membership_id=None,
            granted_by=granted_by,
            reason="test setup",
        )
        await repository.insert_casbin_g_row(
            session, user_id=user_id, role_id=role_id, domain=domain
        )
        await repository.bump_policy_version(session)
    await engine.dispose()


async def reload_enforcer(app) -> None:  # type: ignore[no-untyped-def]
    await app.state.authz_enforcer.reload()


def override_all_as(app, sf_uid: UUID, *, acr: int = 0, auth_time: float | None = None) -> None:
    """Overrides every auth dependency in ``app.routes`` to resolve to the same
    actor — both the plain ``AuthenticatedUser()`` alias and every
    ``Authorize(...)`` alias (list/assign/revoke, org and platform)."""
    from app import routes

    principal = user_principal(sf_uid, acr=acr, auth_time=auth_time)
    for name in (
        "require_authenticated_user",
        "require_read_role_assignment",
        "require_assign_role_assignment",
        "require_revoke_role_assignment",
        "require_read_platform_role_assignment",
        "require_assign_platform_role_assignment",
        "require_revoke_platform_role_assignment",
    ):
        app.dependency_overrides[getattr(routes, name)] = lambda principal=principal: principal
