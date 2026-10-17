"""Signed keyset pagination cursors (LLD-09 §3): ``(sort_key, id)``, tamper-evident.

Offset pagination is reserved for capped relevance search (LLD-09 §3); list
endpoints page by this cursor instead.
"""

import base64
import hashlib
import hmac
import json
from uuid import UUID


class InvalidCursor(ValueError):
    pass


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(data: str) -> bytes:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded)


def encode_cursor(secret: bytes, sort_key: str | int, after_id: UUID) -> str:
    payload = _b64url_encode(json.dumps([sort_key, str(after_id)], separators=(",", ":")).encode())
    signature = _b64url_encode(hmac.new(secret, payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{signature}"


def decode_cursor(secret: bytes, cursor: str) -> tuple[str | int, UUID]:
    try:
        payload, signature = cursor.split(".", 1)
    except ValueError:
        raise InvalidCursor(cursor) from None

    expected = _b64url_encode(hmac.new(secret, payload.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(signature, expected):
        raise InvalidCursor(cursor)

    try:
        sort_key, id_str = json.loads(_b64url_decode(payload))
        return sort_key, UUID(id_str)
    except (ValueError, TypeError) as exc:
        raise InvalidCursor(cursor) from exc
