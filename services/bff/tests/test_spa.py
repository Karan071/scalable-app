"""Unit tests for spa.py's static/SPA-fallback route (LLD-15 §11 "as built").

A minimal app wrapping just this router, same "exercise the real route
logic without the full app" pattern as test_proxy.py -- no settings, Redis,
or OIDC needed.
"""

from pathlib import Path

from app.spa import build_spa_router, resolve_asset
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _client(dist_dir: Path) -> TestClient:
    app = FastAPI()
    app.include_router(build_spa_router(dist_dir))
    return TestClient(app)


def _write_dist(tmp_path: Path) -> Path:
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<html>spa shell</html>")
    (tmp_path / "assets" / "index-abc123.js").write_text("console.log('hi')")
    return tmp_path


def test_root_serves_index(tmp_path: Path) -> None:
    resp = _client(_write_dist(tmp_path)).get("/")
    assert resp.status_code == 200
    assert "spa shell" in resp.text


def test_existing_asset_served_as_is(tmp_path: Path) -> None:
    resp = _client(_write_dist(tmp_path)).get("/assets/index-abc123.js")
    assert resp.status_code == 200
    assert "console.log" in resp.text


def test_client_side_route_falls_back_to_index(tmp_path: Path) -> None:
    """E.g. `/s/:orgId` -- a real path on the SPA's own router, not a file
    on disk. Same `try_files $uri /index.html` behavior any static host
    gives a client-side-routed SPA."""
    resp = _client(_write_dist(tmp_path)).get("/s/some-org-id/products/some-slug")
    assert resp.status_code == 200
    assert "spa shell" in resp.text


def test_resolve_asset_blocks_dot_dot_traversal_outside_dist_dir(tmp_path: Path) -> None:
    """FastAPI's ``{full_path:path}`` converter passes ``..`` segments
    through unresolved -- this is the guard, not URL normalization
    upstream (which an HTTP client may or may not even do)."""
    dist_dir = _write_dist(tmp_path)
    secret = tmp_path.parent / "outside-dist-dir.txt"
    secret.write_text("not servable")

    assert resolve_asset(dist_dir, "../outside-dist-dir.txt") is None
    assert resolve_asset(dist_dir, "assets/../../outside-dist-dir.txt") is None


def test_resolve_asset_returns_none_for_missing_file(tmp_path: Path) -> None:
    assert resolve_asset(_write_dist(tmp_path), "no/such/file.js") is None
