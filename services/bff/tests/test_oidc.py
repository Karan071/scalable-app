import base64
import hashlib

from app.oidc import generate_pkce


def test_pkce_challenge_is_s256_of_verifier() -> None:
    pkce = generate_pkce()

    expected = base64.urlsafe_b64encode(hashlib.sha256(pkce.verifier.encode()).digest())
    expected = expected.rstrip(b"=").decode()

    assert pkce.challenge == expected


def test_pkce_verifier_is_long_enough_for_the_spec() -> None:
    # RFC 7636: verifier must be 43-128 characters.
    pkce = generate_pkce()
    assert 43 <= len(pkce.verifier) <= 128


def test_each_call_gets_a_fresh_verifier() -> None:
    assert generate_pkce().verifier != generate_pkce().verifier
