"""M2 exit evidence: the tenant-isolation matrix (LLD-14 §4).

    make test-system

Real orgs-svc, authz-svc, audit-svc and users-svc on the test environment,
with the fake IdP. The matrix is generated, not hand-listed: every route
whose published guard (``x-sf-authz`` in its OpenAPI, from sf_authz) is
``Authorize(..., domain=org:{org_id})`` is called by six actors:

- the admin of another org;
- a member of the org without the route's permission;
- an authenticated user with no membership;
- a customer;
- an org admin whose membership is disabled;
- the admin of a suspended org.

Each route is called with three request shapes:

- **own**: the actor's own org in the path, and its own resources;
- **other**: another org in the path, and that org's resources;
- **mixed**: the actor's own org in the path, with another org's resource
  IDs in the path or body.

Every cell must be rejected with 403/404. A foreign ID only in the body may
also get 409/422, which is indistinguishable from an unknown ID. No cell may
return 2xx, and afterwards no org's members, invitations or role
assignments have changed.

The one cell that should succeed (an admin calling their own org) is the
positive control. It is only called for reads, so a matrix that fails for
the wrong reason (e.g. no policy loaded) can't pass.
"""

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from uuid import UUID, uuid7

import asyncpg
import httpx
import pytest
import pytest_asyncio
from harness import (
    ENV,
    TEST_DB,
    FakeIdP,
    ServiceProcess,
    any_super_user,
    invitation_token,
    service_env,
    wait_until,
)

pytestmark = pytest.mark.asyncio(loop_scope="module")

_PORTS = {"orgs-svc": 18141, "authz-svc": 18142, "audit-svc": 18143, "users-svc": 18144}
_ORG_DOMAIN = "org:{org_id}"
_DENIED = frozenset({403, 404})
#: A foreign ID in the body (not the path) may also fail validation of the
#: reference, with the same answer as for an ID that doesn't exist.
_BODY_REF_REJECTED = _DENIED | {409, 422}
_PROPAGATION_TIMEOUT_S = 30.0

#: Routes under /v1/orgs/{org_id} that aren't ``Authorize(...)`` org routes,
#: and why the matrix doesn't apply to them.
_NOT_IN_MATRIX = {
    ("POST", "/v1/orgs/{org_id}/members/me:leave"): (
        "self-scoped: acts only on the caller's own membership "
        "(test_leave_is_scoped_to_the_callers_own_membership)"
    ),
}


@dataclass
class Org:
    org_id: str
    owner: UUID
    #: A member other than the actor under test, their role assignment, and
    #: a pending invitation: the resources other actors aim at.
    member_user_id: str = ""
    membership_id: str = ""
    assignment_id: str = ""
    assignment_version: int = 0
    invitation_id: str = ""


@dataclass(frozen=True)
class Route:
    service: str
    method: str
    path: str
    resource: str
    action: str
    operation: dict = field(hash=False, compare=False)

    @property
    def label(self) -> str:
        return f"{self.method} {self.path}"


@dataclass(frozen=True)
class Actor:
    name: str
    user_id: UUID
    #: The org the actor belongs to (None: no membership anywhere).
    home: Org | None
    #: An org that isn't theirs.
    away: Org
    #: True only for the admin of another org: allowed at home (the control).
    allowed_at_home: bool = False


@dataclass(frozen=True)
class Call:
    method: str
    url: str
    headers: dict[str, str]
    body: dict | None
    #: Which resource refs the request carries besides the org in the path.
    path_refs: bool
    body_refs: bool


class Api:
    def __init__(self, idp: FakeIdP, http: httpx.AsyncClient, spec: dict[str, dict]) -> None:
        self.idp = idp
        self.http = http
        self.base = {svc: f"http://127.0.0.1:{port}" for svc, port in _PORTS.items()}
        self.spec = spec

    async def call(self, method: str, url: str, uid: UUID, **kwargs: object) -> httpx.Response:
        token = self.idp.user_token(uid, email=email(uid))
        headers = {"authorization": f"Bearer {token}", **kwargs.pop("headers", {})}  # type: ignore[dict-item]
        return await self.http.request(method, url, headers=headers, **kwargs)  # type: ignore[arg-type]

    async def ok(self, method: str, url: str, uid: UUID, **kwargs: object) -> httpx.Response:
        resp = await self.call(method, url, uid, **kwargs)
        assert resp.status_code == 200, f"{method} {url} -> {resp.status_code}: {resp.text[:300]}"
        return resp

    async def until_status(self, url: str, uid: UUID, expected: int) -> None:
        async def check() -> bool:
            return (await self.call("GET", url, uid)).status_code == expected

        await wait_until(check, timeout_s=_PROPAGATION_TIMEOUT_S, interval_s=0.2)

    def orgs(self, path: str) -> str:
        return self.base["orgs-svc"] + path

    def authz(self, path: str) -> str:
        return self.base["authz-svc"] + path

    def audit(self, path: str) -> str:
        return self.base["audit-svc"] + path


def email(uid: UUID) -> str:
    return f"ti-{uid}@example.com"


# --- the matrix, generated from the published route metadata -------------------------------


def org_routes(spec: dict[str, dict]) -> list[Route]:
    routes = []
    for service, doc in spec.items():
        for path, operations in doc["paths"].items():
            for method, op in operations.items():
                for guard in op.get("x-sf-authz", []):
                    if guard["marker"] == "authorize" and guard["domain"] == _ORG_DOMAIN:
                        routes.append(
                            Route(
                                service,
                                method.upper(),
                                path,
                                guard["resource"],
                                guard["action"],
                                op,
                            )
                        )
    return sorted(routes, key=lambda r: (r.service, r.path, r.method))


def build_call(api: Api, route: Route, *, path_org: Org, res_org: Org) -> Call:
    """The request for ``route`` against ``path_org``'s URL, referencing
    ``res_org``'s resources. Every required parameter and body field must
    be one the matrix knows how to fill; a new one fails loudly here."""
    path_values = {
        "org_id": path_org.org_id,
        "membership_id": res_org.membership_id,
        "invitation_id": res_org.invitation_id,
        "assignment_id": res_org.assignment_id,
    }
    header_values = {"If-Match": str(res_org.assignment_version)}
    body_values: dict[str, object] = {
        "user_id": res_org.member_user_id,
        "membership_id": res_org.membership_id,
        "role_id": "catalog_manager",
        "roles_requested": ["catalog_manager"],
        "email": f"ti-invitee-{uuid.uuid4().hex[:12]}@example.com",
    }
    body_refs = {"user_id", "membership_id"}

    url, path_refs, uses_body_refs = route.path, False, False
    headers: dict[str, str] = {}
    for param in route.operation.get("parameters", []):
        name, where = param["name"], param["in"]
        if where == "path":
            assert name in path_values, f"{route.label}: no matrix value for path param {name!r}"
            url = url.replace(f"{{{name}}}", path_values[name])
            path_refs |= name != "org_id"
        elif where == "header" and param.get("required"):
            assert name in header_values, f"{route.label}: no matrix value for header {name!r}"
            headers[name] = header_values[name]
        elif param.get("required"):
            raise AssertionError(f"{route.label}: no matrix value for {where} param {name!r}")

    body = None
    if "requestBody" in route.operation:
        schema = _resolve(api.spec[route.service], route.operation["requestBody"])
        body = {}
        for prop in schema.get("required", []):
            assert prop in body_values, f"{route.label}: no matrix value for body field {prop!r}"
            body[prop] = body_values[prop]
            uses_body_refs |= prop in body_refs
    return Call(
        route.method, api.base[route.service] + url, headers, body, path_refs, uses_body_refs
    )


def _resolve(doc: dict, request_body: dict) -> dict:
    schema = request_body["content"]["application/json"]["schema"]
    while "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        schema = doc["components"]["schemas"][name]
    return schema


@dataclass(frozen=True)
class Cell:
    actor: Actor
    shape: str  # "own" | "other" | "mixed"
    route: Route
    call: Call
    allowed: frozenset[int]


def cells(api: Api, routes: list[Route], actors: list[Actor]) -> list[Cell]:
    out = []
    for route in routes:
        for actor in actors:
            shapes = [("other", actor.away, actor.away)]
            if actor.home is not None:
                if not actor.allowed_at_home:
                    shapes.append(("own", actor.home, actor.home))
                shapes.append(("mixed", actor.home, actor.away))
            for shape, path_org, res_org in shapes:
                call = build_call(api, route, path_org=path_org, res_org=res_org)
                if shape == "mixed" and not (call.path_refs or call.body_refs):
                    continue  # nothing foreign to reference: same as "own"
                foreign_body_only = shape == "mixed" and not call.path_refs
                allowed = _BODY_REF_REJECTED if foreign_body_only else _DENIED
                out.append(Cell(actor, shape, route, call, allowed))
    return out


async def run(api: Api, cell: Cell, limit: asyncio.Semaphore) -> int:
    c = cell.call
    async with limit:
        resp = await api.call(c.method, c.url, cell.actor.user_id, headers=c.headers, json=c.body)
    return resp.status_code


# --- fixture: three orgs, six actors, built through the real APIs ------------------------


@dataclass
class World:
    api: Api
    routes: list[Route]
    actors: list[Actor]
    orgs: dict[str, Org]
    #: Owner-side controls: (admin, their org) pairs that must be allowed.
    controls: list[tuple[UUID, Org]]


async def create_org(api: Api, admin: UUID, name: str) -> Org:
    owner = uuid7()
    org = await api.ok(
        "POST",
        api.orgs("/v1/platform/orgs"),
        admin,
        json={
            "slug": f"ti-{uuid.uuid4().hex[:16]}",
            "name": f"Tenant isolation {name}",
            "currency": "usd",
            "owner_email": email(owner),
        },
    )
    org_id = org.json()["id"]
    await accept(api, org_id, owner)
    # org_admin arrives through the real pipeline (membership.created ->
    # authz-svc), then reaches each service's enforcer.
    for url in (
        api.authz(f"/v1/orgs/{org_id}/role-assignments"),
        api.orgs(f"/v1/orgs/{org_id}/invitations"),
        api.audit(f"/v1/orgs/{org_id}/audit-events"),
    ):
        await api.until_status(url, owner, 200)
    return Org(org_id, owner)


async def accept(api: Api, org_id: str, uid: UUID) -> None:
    token = await invitation_token(org_id, email(uid))
    await api.ok("POST", api.orgs("/v1/invitations:accept"), uid, json={"token": token})


async def add_member(api: Api, org: Org, role: str) -> tuple[UUID, str]:
    uid = uuid7()
    await api.ok(
        "POST",
        api.orgs(f"/v1/orgs/{org.org_id}/invitations"),
        org.owner,
        json={"email": email(uid), "roles_requested": [role]},
    )
    await accept(api, org.org_id, uid)

    async def granted() -> bool:
        return await assignment_of(api, org, uid, role) is not None

    await wait_until(granted, timeout_s=_PROPAGATION_TIMEOUT_S, interval_s=0.2)
    members = (await api.ok("GET", api.orgs(f"/v1/orgs/{org.org_id}/members"), org.owner)).json()
    return uid, next(m["id"] for m in members if m["user_id"] == str(uid))


async def assignment_of(api: Api, org: Org, uid: UUID, role: str) -> dict | None:
    resp = await api.ok(
        "GET",
        api.authz(f"/v1/orgs/{org.org_id}/role-assignments"),
        org.owner,
        params={"user_id": str(uid)},
    )
    return next((a for a in resp.json() if a["role_id"] == role and a["status"] == "active"), None)


async def set_target(api: Api, org: Org, uid: UUID, membership_id: str, role: str) -> None:
    """Points ``org``'s resources at ``uid``: their membership, their role
    assignment, and a fresh pending invitation."""
    assignment = await assignment_of(api, org, uid, role)
    assert assignment is not None
    invitation = await api.ok(
        "POST",
        api.orgs(f"/v1/orgs/{org.org_id}/invitations"),
        org.owner,
        json={
            "email": f"ti-pending-{uuid.uuid4().hex[:12]}@example.com",
            "roles_requested": ["catalog_manager"],
        },
    )
    org.member_user_id = str(uid)
    org.membership_id = membership_id
    org.assignment_id = assignment["id"]
    org.assignment_version = assignment["version"]
    org.invitation_id = invitation.json()["invitation_id"]


async def provision_customer(api: Api) -> UUID:
    resp = await api.http.post(
        api.base["users-svc"] + "/internal/v1/users:provision",
        headers={
            "authorization": "Bearer "
            + api.idp.service_token("sf-svc-bff", scope="users:provision")
        },
        json={
            "idp_issuer": api.idp.issuer,
            "idp_subject": str(uuid.uuid4()),
            "email": f"ti-customer-{uuid.uuid4().hex[:12]}@example.com",
            "email_verified": True,
        },
    )
    assert resp.status_code == 200, resp.text
    uid = UUID(resp.json()["user_id"])

    async def is_customer() -> bool:
        mine = await api.ok("GET", api.authz("/v1/me/role-assignments"), uid)
        return any(a["role_id"] == "customer" for a in mine.json())

    # user.created -> authz-svc grants role:customer (LLD-02 §9).
    await wait_until(is_customer, timeout_s=_PROPAGATION_TIMEOUT_S, interval_s=0.2)
    return uid


async def own_reads_denied(api: Api, routes: list[Route], uid: UUID, org: Org) -> bool:
    for route in routes:
        if route.method == "GET":
            call = build_call(api, route, path_org=org, res_org=org)
            if (await api.call("GET", call.url, uid)).status_code not in _DENIED:
                return False
    return True


async def build_world(api: Api) -> World:
    admin = await any_super_user()
    # A fresh bootstrap reaches running services on their 10 s policy poll.
    await api.until_status(api.orgs("/v1/platform/orgs"), admin, 200)
    target, other, suspended = await asyncio.gather(
        create_org(api, admin, "target"),
        create_org(api, admin, "other"),
        create_org(api, admin, "suspended"),
    )
    routes = org_routes(api.spec)

    # target: a catalog_manager (lacks every M2 org permission) and an
    # org_admin whose membership will be disabled.
    (catalog, catalog_mid), (disabled, disabled_mid) = await asyncio.gather(
        add_member(api, target, "catalog_manager"), add_member(api, target, "org_admin")
    )
    await set_target(api, target, catalog, catalog_mid, "catalog_manager")
    for org in (other, suspended):
        members = (
            await api.ok("GET", api.orgs(f"/v1/orgs/{org.org_id}/members"), org.owner)
        ).json()
        await set_target(api, org, org.owner, members[0]["id"], "org_admin")

    customer = await provision_customer(api)

    await api.ok(
        "POST",
        api.orgs(f"/v1/orgs/{target.org_id}/members/{disabled_mid}:disable"),
        target.owner,
        json={"reason": "tenant-isolation matrix"},
    )
    await api.ok(
        "POST",
        api.orgs(f"/v1/platform/orgs/{suspended.org_id}:suspend"),
        admin,
        json={"reason": "tenant-isolation matrix"},
    )
    # Both reach each service's Casbin through events: wait until every
    # service denies them, so the matrix sees steady state. If one never
    # does, carry on: the matrix then names exactly which cells let them in.
    for uid, org in ((disabled, target), (suspended.owner, suspended)):

        async def denied(uid: UUID = uid, org: Org = org) -> bool:
            return await own_reads_denied(api, routes, uid, org)

        with contextlib.suppress(TimeoutError):
            await wait_until(denied, timeout_s=_PROPAGATION_TIMEOUT_S, interval_s=0.2)

    actors = [
        Actor("admin of another org", other.owner, home=other, away=target, allowed_at_home=True),
        Actor("member without the permission", catalog, home=target, away=other),
        Actor("non-member", uuid7(), home=None, away=target),
        Actor("customer", customer, home=None, away=target),
        Actor("disabled membership", disabled, home=target, away=other),
        Actor("admin of a suspended org", suspended.owner, home=suspended, away=target),
    ]
    return World(
        api,
        routes,
        actors,
        {"target": target, "other": other, "suspended": suspended},
        controls=[(target.owner, target), (other.owner, other)],
    )


#: (db role, table, columns that change on any write, org column)
_SNAPSHOT = (
    ("svc_orgs", "orgs.organizations", "status, version", "id"),
    ("svc_orgs", "orgs.memberships", "status, version", "org_id"),
    ("svc_orgs", "orgs.invitations", "status, expires_at", "org_id"),
    ("svc_authz", "authz.role_assignments", "status, version", "org_id"),
)


async def snapshot(orgs: list[Org]) -> dict[str, list[tuple]]:
    """Every row a matrix call could change, in the given orgs."""
    ids = [UUID(o.org_id) for o in orgs]
    rows: dict[str, list[tuple]] = {}
    for role, table, columns, org_column in _SNAPSHOT:
        conn = await asyncpg.connect(
            host="localhost", database=TEST_DB, user=role, password=ENV["POSTGRES_APP_PASSWORD"]
        )
        try:
            query = f"SELECT id, {columns} FROM {table} WHERE {org_column} = ANY($1) ORDER BY id"
            rows[table] = [tuple(r) for r in await conn.fetch(query, ids)]
        finally:
            await conn.close()
    return rows


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def world() -> AsyncIterator[World]:
    idp = FakeIdP()
    orgs_url = f"http://127.0.0.1:{_PORTS['orgs-svc']}"
    processes = [
        ServiceProcess(
            svc,
            port=port,
            label=f"{svc}-isolation",
            env=service_env(svc, idp, SF_ORGS_SVC_BASE_URL=orgs_url),
        )
        for svc, port in _PORTS.items()
    ]
    try:
        await asyncio.gather(*(p.start() for p in processes))
        async with httpx.AsyncClient(timeout=30.0) as http:
            spec = {
                p.service: (await http.get(f"{p.base_url}/openapi.json")).json() for p in processes
            }
            yield await build_world(Api(idp, http, spec))
    finally:
        for p in processes:
            p.stop()
        idp.stop()


# --- tests ---------------------------------------------------------------------------------


async def test_every_org_route_is_in_the_matrix(world: World) -> None:
    """The matrix is only as good as its route list: everything under an org
    is either an ``Authorize`` org route (in the matrix) or explained."""
    in_matrix = {(r.method, r.path) for r in world.routes}
    org_paths = {
        (method.upper(), path)
        for doc in world.api.spec.values()
        for path, operations in doc["paths"].items()
        if path.startswith("/v1/orgs/{org_id}")
        for method in operations
    }
    assert org_paths - in_matrix == set(_NOT_IN_MATRIX)
    # Every service with org routes contributes (a guard that stopped
    # publishing metadata would silently shrink the matrix).
    assert {r.service for r in world.routes} == {"orgs-svc", "authz-svc", "audit-svc"}
    assert not [
        (path, method)
        for doc in world.api.spec.values()
        for path, operations in doc["paths"].items()
        for method, op in operations.items()
        for guard in op.get("x-sf-authz", [])
        if guard.get("domain") == "dynamic"
    ], "a domain resolver the matrix can't describe; use sf_authz.org_domain_from_path()"


async def test_the_member_without_the_permission_really_lacks_it(world: World) -> None:
    actor = next(a for a in world.actors if a.name == "member without the permission")
    assert actor.home is not None
    resp = await world.api.ok(
        "GET",
        world.api.authz("/v1/me/permissions"),
        actor.user_id,
        params={"domain": f"org:{actor.home.org_id}"},
    )
    held = set(resp.json()["permissions"])
    for route in world.routes:
        assert not held & {f"{route.resource}:{route.action}", f"{route.resource}:*", "*:*"}, (
            f"{route.label}: catalog_manager holds {route.resource}:{route.action}; "
            "pick a role without it for this actor"
        )


async def test_admins_can_read_their_own_org(world: World) -> None:
    """Positive control: without it, a matrix of denials proves nothing."""
    reads = [r for r in world.routes if r.method == "GET"]
    assert reads
    failures = []
    for uid, org in world.controls:
        for route in reads:
            call = build_call(world.api, route, path_org=org, res_org=org)
            resp = await world.api.call("GET", call.url, uid)
            if resp.status_code != 200:
                failures.append(f"{route.label} -> {resp.status_code}")
    assert failures == []


async def test_no_actor_reaches_another_tenant(world: World) -> None:
    before = await snapshot(list(world.orgs.values()))
    assert world.routes, "no org routes found: is sf_authz publishing x-sf-authz?"
    matrix = cells(world.api, world.routes, world.actors)
    # Every actor meets every route in another org, and resource-taking
    # routes get the mixed shape too.
    assert {(c.actor.name, c.route) for c in matrix if c.shape == "other"} == {
        (a.name, r) for a in world.actors for r in world.routes
    }
    assert any(c.shape == "mixed" and c.call.path_refs for c in matrix)
    assert any(c.shape == "mixed" and c.call.body_refs for c in matrix)

    # Bounded: a 503 from an exhausted DB pool would be a false failure.
    limit = asyncio.Semaphore(8)
    statuses = await asyncio.gather(*(run(world.api, cell, limit) for cell in matrix))
    failures = [
        f"{cell.actor.name} / {cell.shape}: {cell.route.label} -> {status} "
        f"(expected {sorted(cell.allowed)})"
        for cell, status in zip(matrix, statuses, strict=True)
        if status not in cell.allowed
    ]
    assert failures == [], "\n".join(failures)
    assert await snapshot(list(world.orgs.values())) == before


async def test_leave_is_scoped_to_the_callers_own_membership(world: World) -> None:
    """``me:leave`` has no Authorize (anyone may leave their own org), so it
    isn't in the matrix: an outsider "leaving" an org must find nothing."""
    target = world.orgs["target"]
    before = await snapshot([target])
    for actor in world.actors:
        if actor.home is not target:
            resp = await world.api.call(
                "POST", world.api.orgs(f"/v1/orgs/{target.org_id}/members/me:leave"), actor.user_id
            )
            assert resp.status_code == 404, f"{actor.name}: {resp.status_code}"
    assert await snapshot([target]) == before
