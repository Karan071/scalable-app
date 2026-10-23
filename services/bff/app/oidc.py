"""OIDC client: discovery, authorization URL, code exchange, id_token validation (LLD-01 §4)."""

import base64
import hashlib
import secrets
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
from sf_authz import Principal, TokenValidationError, TokenValidator


class IdTokenInvalid(Exception):
    pass


@dataclass(frozen=True)
class DiscoveryDocument:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    end_session_endpoint: str
    jwks_uri: str


@dataclass(frozen=True)
class PkceChallenge:
    verifier: str
    challenge: str  # S256(verifier)


def generate_pkce() -> PkceChallenge:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return PkceChallenge(verifier=verifier, challenge=challenge)


async def discover(http_client: httpx.AsyncClient, issuer: str) -> DiscoveryDocument:
    resp = await http_client.get(f"{issuer}/.well-known/openid-configuration")
    resp.raise_for_status()
    doc = resp.json()
    return DiscoveryDocument(
        issuer=doc["issuer"],
        authorization_endpoint=doc["authorization_endpoint"],
        token_endpoint=doc["token_endpoint"],
        end_session_endpoint=doc["end_session_endpoint"],
        jwks_uri=doc["jwks_uri"],
    )


class OidcClient:
    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient,
        discovery: DiscoveryDocument,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        clock_leeway_s: int = 60,
    ) -> None:
        self._http = http_client
        self._discovery = discovery
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._clock_leeway_s = clock_leeway_s
        self._validator = TokenValidator(issuer=discovery.issuer, jwks_uri=discovery.jwks_uri)

    def build_authorize_url(
        self, *, state: str, nonce: str, code_challenge: str, acr_values: str | None = None
    ) -> str:
        params = {
            "response_type": "code",
            "client_id": self._client_id,
            "redirect_uri": self._redirect_uri,
            "scope": "openid",
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        if acr_values:
            # No max_age=0: Keycloak would re-run every level, so a step-up
            # cost password + OTP + OTP. The realm's level-2 condition has
            # max-age 0, so the second factor is re-prompted every time; the
            # callback checks auth_time advanced (LLD-01 §3.3, §9).
            params["acr_values"] = acr_values
        return f"{self._discovery.authorization_endpoint}?{urlencode(params)}"

    async def end_session(self, *, refresh_token: str) -> None:
        """Ends the Keycloak session server-side: Keycloak's end-session
        endpoint accepts a confidential client's refresh token. Redirecting
        the browser there instead would need ``id_token_hint`` in the URL,
        putting the ID token in the browser (LLD-01 §8, §16)."""
        resp = await self._http.post(
            self._discovery.end_session_endpoint,
            data={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "refresh_token": refresh_token,
            },
        )
        resp.raise_for_status()

    async def exchange_code(self, *, code: str, code_verifier: str) -> dict[str, object]:
        resp = await self._http.post(
            self._discovery.token_endpoint,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self._redirect_uri,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "code_verifier": code_verifier,
            },
        )
        resp.raise_for_status()
        return resp.json()

    async def refresh(self, refresh_token: str) -> dict[str, object]:
        resp = await self._http.post(
            self._discovery.token_endpoint,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
        )
        resp.raise_for_status()
        return resp.json()

    def validate_id_token(
        self, id_token: str, *, nonce: str | None, acr_req: str | None = None
    ) -> Principal:
        """``nonce=None`` skips the nonce check: a refresh-grant id_token isn't required to
        carry the original login's nonce (OIDC Core §12.2 only mandates it on the id_token
        returned directly from the authorization endpoint flow)."""
        try:
            principal = self._validator.validate(
                id_token, audience=self._client_id, leeway_s=self._clock_leeway_s
            )
        except TokenValidationError as exc:
            raise IdTokenInvalid(str(exc)) from exc

        if nonce is not None and principal.claim("nonce") != nonce:
            raise IdTokenInvalid("nonce mismatch")
        if principal.claim("azp") not in (self._client_id, None):
            raise IdTokenInvalid("azp mismatch")
        if acr_req is not None and str(principal.claim("acr")) < acr_req:
            raise IdTokenInvalid("acr below required level")
        return principal
