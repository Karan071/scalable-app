"""Thin admin-API client for the Compose `toxiproxy` container (LLD-13 §3/§4,
E4.8). Proxies and toxics are created/removed per test, never pre-declared --
`deploy/compose/compose.yaml`'s own comment explains why the listen ports are
a fixed published block instead of a dynamic range.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from types import TracebackType

import httpx

ADMIN_URL = "http://localhost:8474"


class ToxiproxyClient:
    def __init__(self, admin_url: str = ADMIN_URL) -> None:
        self._http = httpx.AsyncClient(base_url=admin_url, timeout=3.0)

    async def reachable(self) -> bool:
        try:
            resp = await self._http.get("/version")
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    async def create_proxy(self, name: str, *, listen_port: int, upstream_port: int) -> Proxy:
        """``upstream`` is on the Docker host (a `tests/system` service
        process, not a container), hence `host.docker.internal` -- see the
        compose comment."""
        await self.delete_proxy(name)
        resp = await self._http.post(
            "/proxies",
            json={
                "name": name,
                "listen": f"0.0.0.0:{listen_port}",
                "upstream": f"host.docker.internal:{upstream_port}",
            },
        )
        resp.raise_for_status()
        return Proxy(self, name, listen_port=listen_port)

    async def delete_proxy(self, name: str) -> None:
        with contextlib.suppress(httpx.HTTPStatusError):
            resp = await self._http.delete(f"/proxies/{name}")
            if resp.status_code not in (204, 404):
                resp.raise_for_status()

    async def add_toxic(
        self,
        proxy_name: str,
        *,
        toxic_name: str,
        toxic_type: str,
        stream: str = "upstream",
        attributes: dict[str, object],
    ) -> None:
        resp = await self._http.post(
            f"/proxies/{proxy_name}/toxics",
            json={
                "name": toxic_name,
                "type": toxic_type,
                "stream": stream,
                "attributes": attributes,
            },
        )
        resp.raise_for_status()

    async def remove_toxic(self, proxy_name: str, toxic_name: str) -> None:
        with contextlib.suppress(httpx.HTTPStatusError):
            await self._http.delete(f"/proxies/{proxy_name}/toxics/{toxic_name}")

    async def aclose(self) -> None:
        await self._http.aclose()


class Proxy:
    """A created proxy, with convenience methods for the toxics E4.8 needs."""

    def __init__(self, client: ToxiproxyClient, name: str, *, listen_port: int) -> None:
        self._client = client
        self.name = name
        self.base_url = f"http://localhost:{listen_port}"

    async def add_latency(self, *, latency_ms: int, jitter_ms: int = 0) -> None:
        # "downstream" = the upstream server's response back to the client
        # -- a slow-to-respond dependency, not a slow request upload.
        await self._client.add_toxic(
            self.name,
            toxic_name="latency",
            toxic_type="latency",
            stream="downstream",
            attributes={"latency": latency_ms, "jitter": jitter_ms},
        )

    async def remove_latency(self) -> None:
        await self._client.remove_toxic(self.name, "latency")

    async def __aenter__(self) -> Proxy:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._client.delete_proxy(self.name)


@contextlib.asynccontextmanager
async def proxy(
    client: ToxiproxyClient, name: str, *, listen_port: int, upstream_port: int
) -> AsyncIterator[Proxy]:
    p = await client.create_proxy(name, listen_port=listen_port, upstream_port=upstream_port)
    try:
        yield p
    finally:
        await client.delete_proxy(name)
