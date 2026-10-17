import time

import pytest
from sf_authz import TokenValidationError, TokenValidator

ISSUER = "https://kc.test/realms/scaleforge"


def _claims(**overrides: object) -> dict[str, object]:
    now = int(time.time())
    base = {
        "iss": ISSUER,
        "sub": "user:0192",
        "aud": "scaleforge-api",
        "iat": now,
        "exp": now + 300,
    }
    base.update(overrides)
    return base


def test_validates_a_well_formed_token(jwks_server: str, sign_token) -> None:
    validator = TokenValidator(issuer=ISSUER, jwks_uri=jwks_server)
    token = sign_token(_claims(sf_uid="u-1", acr="1"))

    principal = validator.validate(token, audience="scaleforge-api")

    assert principal.subject == "user:0192"
    assert principal.claim("sf_uid") == "u-1"
    assert principal.audiences == ("scaleforge-api",)


def test_rejects_wrong_audience(jwks_server: str, sign_token) -> None:
    validator = TokenValidator(issuer=ISSUER, jwks_uri=jwks_server)
    token = sign_token(_claims(aud="scaleforge-internal"))

    with pytest.raises(TokenValidationError):
        validator.validate(token, audience="scaleforge-api")


def test_rejects_wrong_issuer(jwks_server: str, sign_token) -> None:
    validator = TokenValidator(issuer=ISSUER, jwks_uri=jwks_server)
    token = sign_token(_claims(iss="https://evil.example/realms/scaleforge"))

    with pytest.raises(TokenValidationError):
        validator.validate(token, audience="scaleforge-api")


def test_rejects_expired_token(jwks_server: str, sign_token) -> None:
    validator = TokenValidator(issuer=ISSUER, jwks_uri=jwks_server)
    now = int(time.time())
    token = sign_token(_claims(iat=now - 400, exp=now - 100))

    with pytest.raises(TokenValidationError):
        validator.validate(token, audience="scaleforge-api")


def test_rejects_unknown_signing_key(jwks_server: str, sign_token) -> None:
    validator = TokenValidator(issuer=ISSUER, jwks_uri=jwks_server)
    token = sign_token(_claims(), kid="some-other-key")

    with pytest.raises(TokenValidationError):
        validator.validate(token, audience="scaleforge-api")


def test_scopes_splits_space_delimited_claim(jwks_server: str, sign_token) -> None:
    validator = TokenValidator(issuer=ISSUER, jwks_uri=jwks_server)
    token = sign_token(_claims(scope="users:provision users:read"))

    principal = validator.validate(token, audience="scaleforge-api")

    assert principal.scopes() == {"users:provision", "users:read"}
