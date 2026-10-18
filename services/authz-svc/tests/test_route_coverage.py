"""Confirms authz-svc's own routes pass the route-coverage guard.

``app.main`` calls ``assert_routes_covered(app)`` at module import time, so
successfully importing it here already proves every route is covered —
this test exists to make that assertion explicit and fast (no DB needed).
"""


def test_main_module_imports_without_a_route_coverage_error() -> None:
    import app.main  # noqa: F401
