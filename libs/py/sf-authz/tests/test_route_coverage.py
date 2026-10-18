import pytest
from fastapi import Depends, FastAPI
from sf_authz import Public
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
