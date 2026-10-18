"""JWT validation against a cached JWKS (ADR-0009 §3, LLD-01 §13)."""

from dataclasses import dataclass, field
from typing import Any

import jwt
from jwt import PyJWKClient

ALLOWED_ALGORITHMS = ("RS256", "ES256")


class TokenValidationError(Exception):
    pass


@dataclass(frozen=True)
class Principal:
    subject: str
    audiences: tuple[str, ...]
    claims: dict[str, Any] = field(default_factory=dict)

    def claim(self, name: str) -> Any | None:
        return self.claims.get(name)

    def scopes(self) -> frozenset[str]:
        return frozenset(str(self.claims.get("scope", "")).split())

    def casbin_subject(self) -> str:
        """``user:<sf_uid>`` (LLD-02 §2). Raises if the token predates sf_uid provisioning."""
        sf_uid = self.claim("sf_uid")
        if sf_uid is None:
            raise TokenValidationError("token has no sf_uid claim yet; refresh required")
        return f"user:{sf_uid}"


class TokenValidator:
    """Validates Keycloak-issued JWTs. One instance per service process.

    JWKS are cached by ``PyJWKClient`` and refetched when a token references
    an unknown ``kid`` (LLD-01 §13's "max 1/min" cap on that refetch is not
    enforced here yet — an open item, not exploitable beyond extra JWKS
    fetches against our own Keycloak).
    """

    def __init__(self, *, issuer: str, jwks_uri: str, jwks_cache_ttl_s: float = 600.0) -> None:
        self._issuer = issuer
        self._jwk_client = PyJWKClient(jwks_uri, lifespan=jwks_cache_ttl_s)

    def validate(self, token: str, *, audience: str, leeway_s: float = 30.0) -> Principal:
        try:
            signing_key = self._jwk_client.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=list(ALLOWED_ALGORITHMS),
                audience=audience,
                issuer=self._issuer,
                leeway=leeway_s,
                options={"require": ["exp", "iat", "sub"]},
            )
        except jwt.PyJWTError as exc:
            raise TokenValidationError(str(exc)) from exc

        aud = claims["aud"]
        audiences = (aud,) if isinstance(aud, str) else tuple(aud)
        return Principal(subject=claims["sub"], audiences=audiences, claims=claims)
