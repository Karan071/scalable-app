import base64

import pytest
from cryptography.exceptions import InvalidTag
from sf_common import TokenCipher


def _cipher() -> TokenCipher:
    key = base64.b64encode(bytes(range(32))).decode()
    return TokenCipher.from_base64_keys({"k1": key}, "k1")


def test_round_trips_plaintext() -> None:
    cipher = _cipher()
    token = cipher.encrypt("super-secret-access-token", aad=b"session-hash")

    assert cipher.decrypt(token, aad=b"session-hash") == "super-secret-access-token"


def test_ciphertext_is_not_the_plaintext() -> None:
    cipher = _cipher()
    token = cipher.encrypt("super-secret-access-token", aad=b"session-hash")

    assert "super-secret-access-token" not in token


def test_wrong_aad_fails_to_decrypt() -> None:
    cipher = _cipher()
    token = cipher.encrypt("super-secret-access-token", aad=b"session-hash")

    with pytest.raises(InvalidTag):
        cipher.decrypt(token, aad=b"different-session-hash")


def test_unknown_kid_fails_to_decrypt() -> None:
    cipher = _cipher()
    token = cipher.encrypt("super-secret-access-token", aad=b"session-hash")
    tampered = "unknown-kid:" + token.split(":", 1)[1]

    with pytest.raises(KeyError):
        cipher.decrypt(tampered, aad=b"session-hash")


def test_rejects_unknown_primary_kid() -> None:
    key = base64.b64encode(bytes(range(32))).decode()
    with pytest.raises(ValueError):
        TokenCipher.from_base64_keys({"k1": key}, "k2")
