"""Startup/CI guard: every route must declare exactly one sf_authz marker (LLD-02 §6, §15)."""

from fastapi import FastAPI
from fastapi.routing import APIRoute

#: LLD-02 §6 names only Authorize/Public/Internal. ``AuthenticatedUser`` is a
#: deliberate 4th marker for routes that are inherently self-scoped and have
#: no Casbin resource/domain to check (e.g. ``GET /v1/me``).
VALID_MARKERS = frozenset({"public", "internal", "authenticated_user", "authorize"})


class RouteCoverageError(Exception):
    pass


def assert_routes_covered(
    app: FastAPI, *, exempt_tags: frozenset[str] = frozenset({"health"})
) -> None:
    """Raises if any route lacks a declared auth dependency.

    Called at module scope right after ``app.include_router(...)``, so an
    undeclared route crashes the process before ``uvicorn`` binds a port.
    """
    offenders: list[str] = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        if exempt_tags & set(route.tags or ()):
            continue
        markers = {
            getattr(dep.call, "sf_authz_marker", None) for dep in route.dependant.dependencies
        }
        if not markers & VALID_MARKERS:
            methods = ",".join(sorted(route.methods or ()))
            offenders.append(f"{methods} {route.path}")
    if offenders:
        raise RouteCoverageError(
            "routes with no sf_authz guard (Authorize/Public/Internal/AuthenticatedUser): "
            + ", ".join(sorted(offenders))
        )
