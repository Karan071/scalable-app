"""Startup/CI guard: every route must declare exactly one sf_authz marker (LLD-02 §6, §15)."""

from collections.abc import Callable, Iterator
from typing import Any

from fastapi import FastAPI
from fastapi.routing import APIRoute, iter_route_contexts

#: LLD-02 §6 names only Authorize/Public/Internal. ``AuthenticatedUser`` is a
#: deliberate 4th marker for routes that are inherently self-scoped and have
#: no Casbin resource/domain to check (e.g. ``GET /v1/me``).
VALID_MARKERS = frozenset({"public", "internal", "authenticated_user", "authorize"})

#: OpenAPI extension carrying each operation's guards (``describe_guard``).
OPENAPI_EXTENSION = "x-sf-authz"

_GUARD_FIELDS = {
    "sf_authz_resource": "resource",
    "sf_authz_action": "action",
    "sf_authz_domain": "domain",
    "sf_authz_min_acr": "min_acr",
    "sf_authz_scope": "scope",
    "sf_authz_rate_class": "rate_class",
}


class RouteCoverageError(Exception):
    pass


def describe_guard(call: Callable[..., object]) -> dict[str, object]:
    """A marker dependency as data, e.g. ``{"marker": "authorize",
    "resource": "member", "action": "read", "domain": "org:{org_id}"}``."""
    described: dict[str, object] = {"marker": call.sf_authz_marker}  # type: ignore[attr-defined]
    for attr, key in _GUARD_FIELDS.items():
        value = getattr(call, attr, None)
        if value is not None:
            described[key] = value
    return described


def _api_routes(app: FastAPI) -> Iterator[Any]:
    """Every API route as served, with router-level dependencies and tags
    applied. Since FastAPI 0.14x, ``app.routes`` holds included routers
    unflattened, so iterating it alone sees none of their routes."""
    for route in iter_route_contexts(app.routes):
        if isinstance(route.original_route, APIRoute):
            yield route


def assert_routes_covered(
    app: FastAPI, *, exempt_tags: frozenset[str] = frozenset({"health"})
) -> None:
    """Raises if any route lacks a declared auth dependency.

    Called at module scope right after ``app.include_router(...)``, so an
    undeclared route crashes the process before ``uvicorn`` binds a port.
    Also publishes each route's guards in its OpenAPI operation as
    ``x-sf-authz``: the security suites are generated from it (LLD-14 §4).
    """
    offenders: list[str] = []
    published: dict[tuple[str, str], list[dict[str, object]]] = {}
    for route in _api_routes(app):
        if exempt_tags & set(route.tags or ()):
            continue
        guards = [
            dep.call
            for dep in route.dependant.dependencies
            if getattr(dep.call, "sf_authz_marker", None) in VALID_MARKERS
        ]
        if not guards:
            methods = ",".join(sorted(route.methods or ()))
            offenders.append(f"{methods} {route.path_format}")
            continue
        for method in route.methods or ():
            published[(route.path_format, method.lower())] = [describe_guard(g) for g in guards]
    if offenders:
        raise RouteCoverageError(
            "routes with no sf_authz guard (Authorize/Public/Internal/AuthenticatedUser): "
            + ", ".join(sorted(offenders))
        )
    _publish_in_openapi(app, published)


def _publish_in_openapi(
    app: FastAPI, published: dict[tuple[str, str], list[dict[str, object]]]
) -> None:
    # FastAPI's documented way to extend the generated schema: wrap app.openapi.
    generate = app.openapi

    def openapi() -> dict[str, Any]:
        schema = generate()
        for (path, method), guards in published.items():
            operation = schema.get("paths", {}).get(path, {}).get(method)
            if operation is not None:
                operation[OPENAPI_EXTENSION] = guards
        return schema

    app.openapi = openapi  # type: ignore[method-assign]
