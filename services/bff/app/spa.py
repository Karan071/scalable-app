"""Local-dev-only static serving for apps/web's build output (LLD-15 §11).

Not used in Kubernetes: LLD-12 §6 routes static assets to a separate `web`
backend at the gateway, same origin as this service only because both sit
behind the same hostname. Locally there is no such gateway, so `/auth
/callback`'s relative-path redirect (e.g. back to `/`) has nowhere to land
without this -- registered last, so it never shadows `/api/**` or `/auth/**`.
"""

from pathlib import Path

from fastapi import APIRouter
from starlette.responses import FileResponse


def resolve_asset(dist_dir: Path, full_path: str) -> Path | None:
    """The file ``full_path`` would serve, or ``None`` if it doesn't exist
    inside ``dist_dir`` -- including a ``full_path`` that tries to escape it
    with ``..`` segments (FastAPI's ``{path}`` converter passes those
    through unresolved, so this, not the router, is what has to block it)."""
    if not full_path:
        return None
    candidate = (dist_dir / full_path).resolve()
    if candidate.is_file() and candidate.is_relative_to(dist_dir):
        return candidate
    return None


def build_spa_router(dist_dir: Path) -> APIRouter:
    router = APIRouter()
    dist_dir = dist_dir.resolve()
    index_path = dist_dir / "index.html"

    @router.get("/{full_path:path}")
    async def serve_spa(full_path: str) -> FileResponse:
        asset = resolve_asset(dist_dir, full_path)
        if asset is not None:
            return FileResponse(asset)
        # Unknown path, or a client-side route (e.g. /s/:orgId): the SPA's
        # own router resolves it, same as any static host's `try_files
        # $uri /index.html`.
        return FileResponse(index_path)

    return router
