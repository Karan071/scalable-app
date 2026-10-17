from uuid import uuid4

import pytest
from sf_db import InvalidCursor, decode_cursor, encode_cursor

SECRET = b"test-secret-key-32-bytes-long!!"


def test_round_trips_sort_key_and_id() -> None:
    item_id = uuid4()
    cursor = encode_cursor(SECRET, "2026-10-17T00:00:00Z", item_id)

    sort_key, decoded_id = decode_cursor(SECRET, cursor)

    assert sort_key == "2026-10-17T00:00:00Z"
    assert decoded_id == item_id


def test_rejects_cursor_signed_with_a_different_secret() -> None:
    cursor = encode_cursor(SECRET, 1, uuid4())

    with pytest.raises(InvalidCursor):
        decode_cursor(b"wrong-secret-key-32-bytes-long!", cursor)


def test_rejects_tampered_payload() -> None:
    cursor = encode_cursor(SECRET, 1, uuid4())
    payload, signature = cursor.split(".", 1)
    tampered = encode_cursor(SECRET, 999, uuid4()).split(".", 1)[0] + "." + signature

    with pytest.raises(InvalidCursor):
        decode_cursor(SECRET, tampered)


def test_rejects_malformed_cursor() -> None:
    with pytest.raises(InvalidCursor):
        decode_cursor(SECRET, "not-a-valid-cursor")
