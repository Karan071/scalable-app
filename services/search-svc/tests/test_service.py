"""Pure unit tests for query validation and cursor handling (LLD-05 §8, §13)."""

import asyncio
import base64
from uuid import uuid4

import pytest
from app import service
from sf_db import InvalidCursor, decode_cursor, encode_cursor

_SECRET_B64 = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="


def _request(**overrides: object) -> service.RawSearchRequest:
    defaults: dict[str, object] = dict(
        q=None,
        category=None,
        price_min=None,
        price_max=None,
        in_stock=None,
        sort=None,
        limit=24,
        cursor=None,
        page=None,
    )
    return service.RawSearchRequest(**{**defaults, **overrides})  # type: ignore[arg-type]


def test_clean_q_strips_control_characters_and_trims() -> None:
    assert service._clean_q("  hello\x00world\x1f  ") == "helloworld"


def test_clean_q_rejects_overlong_queries() -> None:
    with pytest.raises(service.SearchValidationError):
        service._clean_q("x" * 101)


def test_cursor_secret_decodes_base64() -> None:
    assert service.cursor_secret(_SECRET_B64) == base64.b64decode(_SECRET_B64)


@pytest.mark.parametrize(
    "overrides",
    [
        {"sort": "not-a-real-sort"},
        {"limit": 0},
        {"limit": 49},
        {"price_min": -1},
        {"price_max": -1},
        {"price_min": 100, "price_max": 50},
        {"sort": "relevance", "page": 51},
        {"sort": "relevance", "cursor": "anything"},
        {"sort": "newest", "page": 2},
    ],
)
def test_invalid_requests_raise_search_validation_error(overrides: dict[str, object]) -> None:
    with pytest.raises(service.SearchValidationError):
        asyncio.run(
            service.run_search(None, None, _request(**overrides), secret_b64=_SECRET_B64)  # type: ignore[arg-type]
        )


def test_decode_keyset_cursor_rejects_a_tampered_signature() -> None:
    secret = service.cursor_secret(_SECRET_B64)
    cursor = encode_cursor(secret, 100, uuid4())
    tampered = cursor[:-1] + ("A" if cursor[-1] != "A" else "B")

    with pytest.raises(InvalidCursor):
        decode_cursor(secret, tampered)
