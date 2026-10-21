"""Redis-backed session store (LLD-01 §5).

Local Compose runs a single redis-session instance (no Sentinel/replicas —
that's a Kubernetes-only concern per LLD-01 §12), so the `WAIT` call LLD-01
§12 uses to bound replication lag on revocation is skipped here (open item
for the Kubernetes topology, where it has replicas to wait on).
"""

import hashlib
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis
from sf_common import TokenCipher

_SESSION_ID_BYTES = 32  # 256 bits
_IDLE_TOUCH_THRESHOLD_S = 60


@dataclass(frozen=True)
class SessionRecord:
    uid: str
    sub: str
    idp_sid: str
    access_token: str
    refresh_token: str
    id_token: str
    at_exp: int
    acr: str
    auth_time: int
    csrf: str
    created: int
    last_seen: int
    abs_exp: int
    session_class: str
    v: int = 1


def key_hash(session_id: str) -> str:
    return hashlib.sha256(session_id.encode()).hexdigest()


def _redis_key(h: str) -> str:
    return f"sess:{h}"


def _user_index_key(uid: str) -> str:
    return f"user_sessions:{uid}"


def _idp_sid_index_key(idp_sid: str) -> str:
    return f"idp_sid:{idp_sid}"


def _as_str_dict(raw: Mapping[Any, Any]) -> dict[str, str]:
    """``decode_responses=True`` guarantees str at runtime; the stubs still say bytes|str."""
    return {str(k): str(v) for k, v in raw.items()}


class SessionStore:
    def __init__(self, redis: Redis, cipher: TokenCipher) -> None:
        self._redis = redis
        self._cipher = cipher

    async def create(
        self,
        *,
        uid: str,
        sub: str,
        idp_sid: str,
        access_token: str,
        refresh_token: str,
        id_token: str,
        at_exp: int,
        acr: str,
        auth_time: int,
        session_class: str,
        idle_s: int,
        abs_s: int,
    ) -> tuple[str, str]:
        """Returns ``(session_id, csrf_token)``. The session id is never reused across logins."""
        session_id = secrets.token_urlsafe(_SESSION_ID_BYTES)
        h = key_hash(session_id)
        aad = h.encode()
        now = int(time.time())
        csrf_secret = secrets.token_urlsafe(32)

        fields: dict[str, str | int] = {
            "uid": uid,
            "sub": sub,
            "idp_sid": idp_sid,
            "at": self._cipher.encrypt(access_token, aad=aad),
            "rt": self._cipher.encrypt(refresh_token, aad=aad),
            "idt": self._cipher.encrypt(id_token, aad=aad),
            "at_exp": at_exp,
            "acr": acr,
            "auth_time": auth_time,
            "csrf": csrf_secret,
            "created": now,
            "last_seen": now,
            "abs_exp": now + abs_s,
            "class": session_class,
            "v": 1,
        }

        async with self._redis.pipeline(transaction=True) as pipe:
            # redis-py's stub wants an invariant Mapping of its own encodable
            # union; a plain dict[str, str | int] satisfies it at runtime but
            # not structurally.
            pipe.hset(_redis_key(h), mapping=fields)  # type: ignore[arg-type]
            pipe.expire(_redis_key(h), min(idle_s, abs_s))
            pipe.sadd(_user_index_key(uid), h)
            pipe.set(_idp_sid_index_key(idp_sid), h, ex=abs_s)
            await pipe.execute()

        return session_id, csrf_secret

    async def load(self, session_id: str, *, idle_s: int) -> SessionRecord | None:
        h = key_hash(session_id)
        raw = _as_str_dict(await self._redis.hgetall(_redis_key(h)))
        if not raw:
            return None

        now = int(time.time())
        abs_exp = int(raw["abs_exp"])
        if now >= abs_exp:
            await self.delete(session_id)
            return None

        last_seen = int(raw["last_seen"])
        if now - last_seen > _IDLE_TOUCH_THRESHOLD_S:
            new_ttl = min(idle_s, abs_exp - now)
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.hset(_redis_key(h), "last_seen", now)
                pipe.expire(_redis_key(h), new_ttl)
                await pipe.execute()
            last_seen = now

        aad = h.encode()
        return SessionRecord(
            uid=raw["uid"],
            sub=raw["sub"],
            idp_sid=raw["idp_sid"],
            access_token=self._cipher.decrypt(raw["at"], aad=aad),
            refresh_token=self._cipher.decrypt(raw["rt"], aad=aad),
            id_token=self._cipher.decrypt(raw["idt"], aad=aad),
            at_exp=int(raw["at_exp"]),
            acr=raw["acr"],
            auth_time=int(raw["auth_time"]),
            csrf=raw["csrf"],
            created=int(raw["created"]),
            last_seen=last_seen,
            abs_exp=abs_exp,
            session_class=raw["class"],
            v=int(raw["v"]),
        )

    async def peek_access_token(self, session_id: str) -> tuple[str, int] | None:
        """Reads just ``at``/``at_exp`` without touching ``last_seen``/TTL —
        used by the refresh-lock's losing side to pick up a winner's refresh
        (LLD-01 §6) without re-running ``load()``'s idle-touch side effects."""
        h = key_hash(session_id)
        at_enc, at_exp_raw = await self._redis.hmget(_redis_key(h), ["at", "at_exp"])
        if at_enc is None or at_exp_raw is None:
            return None
        aad = h.encode()
        return self._cipher.decrypt(str(at_enc), aad=aad), int(str(at_exp_raw))

    async def update_tokens(
        self, session_id: str, *, access_token: str, refresh_token: str, at_exp: int
    ) -> None:
        """In-place refresh (LLD-01 §6): no session-id rotation, so a
        concurrent request already holding this cookie keeps working. Silently
        no-ops if the session has meanwhile been deleted (e.g. revoked
        elsewhere) — the caller re-reads/re-authenticates on its own next load."""
        h = key_hash(session_id)
        if not await self._redis.exists(_redis_key(h)):
            return
        aad = h.encode()
        await self._redis.hset(
            _redis_key(h),
            mapping={
                "at": self._cipher.encrypt(access_token, aad=aad),
                "rt": self._cipher.encrypt(refresh_token, aad=aad),
                "at_exp": at_exp,
            },
        )

    async def delete(self, session_id: str) -> None:
        h = key_hash(session_id)
        raw = _as_str_dict(await self._redis.hgetall(_redis_key(h)))
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.delete(_redis_key(h))
            if raw:
                pipe.srem(_user_index_key(raw["uid"]), h)
                pipe.delete(_idp_sid_index_key(raw["idp_sid"]))
            await pipe.execute()

    async def delete_all_for_user(self, uid: str) -> int:
        hashes = {str(h) for h in await self._redis.smembers(_user_index_key(uid))}
        if not hashes:
            return 0
        async with self._redis.pipeline(transaction=True) as pipe:
            for h in hashes:
                pipe.delete(_redis_key(h))
            pipe.delete(_user_index_key(uid))
            await pipe.execute()
        return len(hashes)
