import pytest
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.testclient import TestClient
from sf_authz import Authorize, Internal, Public, org_domain_from_path
from sf_authz.route_coverage import RouteCoverageError, assert_routes_covered


def test_passes_when_every_route_has_a_marker() -> None:
    app = FastAPI()

    @app.get("/healthz")
    def health(_: None = Depends(Public(rate_class="public"))) -> dict[str, str]:
        return {"status": "ok"}

    assert_routes_covered(app)  # does not raise


def test_fails_when_a_route_has_no_marker() -> None:
    app = FastAPI()

    @app.get("/healthz")
    def health(_: None = Depends(Public(rate_class="public"))) -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/widgets")
    def widgets() -> dict[str, str]:
        return {}

    with pytest.raises(RouteCoverageError) as exc_info:
        assert_routes_covered(app)

    assert "GET /v1/widgets" in str(exc_info.value)


def test_health_tagged_route_is_exempt() -> None:
    app = FastAPI()

    @app.get("/readyz", tags=["health"])
    def ready() -> dict[str, str]:
        return {"status": "ok"}

    assert_routes_covered(app)  # does not raise despite no marker


def test_publishes_each_guard_in_openapi() -> None:
    app = FastAPI()

    @app.get("/v1/orgs/{org_id}/members")
    def members(
        org_id: str,
        _: object = Depends(Authorize("member", "read", domain=org_domain_from_path())),
    ) -> list[str]:
        return []

    @app.get("/v1/platform/orgs")
    def platform_orgs(
        _: object = Depends(Authorize("platform_org", "read", domain="platform")),
    ) -> list[str]:
        return []

    @app.get("/internal/v1/things")
    def things(_: object = Depends(Internal("things:read"))) -> list[str]:
        return []

    @app.get("/readyz", tags=["health"])
    def ready() -> dict[str, str]:
        return {"status": "ok"}

    assert_routes_covered(app)
    paths = app.openapi()["paths"]

    assert paths["/v1/orgs/{org_id}/members"]["get"]["x-sf-authz"] == [
        {"marker": "authorize", "resource": "member", "action": "read", "domain": "org:{org_id}"}
    ]
    assert paths["/v1/platform/orgs"]["get"]["x-sf-authz"] == [
        {"marker": "authorize", "resource": "platform_org", "action": "read", "domain": "platform"}
    ]
    assert paths["/internal/v1/things"]["get"]["x-sf-authz"] == [
        {"marker": "internal", "scope": "things:read"}
    ]
    assert "x-sf-authz" not in paths["/readyz"]["get"]


def test_org_domain_from_path_reads_the_named_parameter() -> None:
    app = FastAPI()

    @app.get("/v1/orgs/{org_id}")
    def org(request: Request, org_id: str) -> dict[str, str]:
        return {"domain": org_domain_from_path()(request)}

    @app.get("/v1/stores/{store_id}")
    def store(request: Request, store_id: str) -> dict[str, str]:
        return {"domain": org_domain_from_path("store_id")(request)}

    client = TestClient(app)
    assert client.get("/v1/orgs/abc").json() == {"domain": "org:abc"}
    assert client.get("/v1/stores/xyz").json() == {"domain": "org:xyz"}


def test_checks_routes_from_included_routers() -> None:
    """FastAPI 0.14x keeps included routers unflattened in ``app.routes``;
    the guard once saw none of their routes and passed every service."""
    router = APIRouter()

    @router.get("/v1/widgets")
    def widgets() -> dict[str, str]:
        return {}

    app = FastAPI()
    app.include_router(router)

    with pytest.raises(RouteCoverageError) as exc_info:
        assert_routes_covered(app)

    assert "GET /v1/widgets" in str(exc_info.value)


def test_router_level_guards_count() -> None:
    router = APIRouter()

    @router.get("/v1/widgets")
    def widgets() -> dict[str, str]:
        return {}

    app = FastAPI()
    app.include_router(router, dependencies=[Depends(Public(rate_class="public"))])

    assert_routes_covered(app)  # does not raise
    assert app.openapi()["paths"]["/v1/widgets"]["get"]["x-sf-authz"] == [
        {"marker": "public", "rate_class": "public"}
    ]
