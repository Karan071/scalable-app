"""Pre-auth login state (LLD-01 §4): single-use, deleted on first read."""

import json
from typing import Any

from redis.asyncio import Redis

_TTL_S = 600


def _key(state: str) -> str:
    return f"oidc:state:{state}"


async def store(redis: Redis, state: str, data: dict[str, Any]) -> None:
    await redis.set(_key(state), json.dumps(data), ex=_TTL_S)


async def consume(redis: Redis, state: str) -> dict[str, Any] | None:
    raw = await redis.getdel(_key(state))
    return None if raw is None else json.loads(raw)
