"""FastAPI auth dependencies: ``Internal``, ``AuthenticatedUser``, ``Public`` (LLD-02 §6).

``Authorize(...)`` (full Casbin enforcement) lands with authz-svc in E2.7.
"""

from collections.abc import Callable

from fastapi import FastAPI, HTTPException, Request

from .tokens import Principal, TokenValidationError, TokenValidator

USER_AUDIENCE = "scaleforge-api"
INTERNAL_AUDIENCE = "scaleforge-internal"


def install_token_validator(app: FastAPI, validator: TokenValidator) -> None:
    app.state.token_validator = validator


def _bearer_token(request: Request) -> str:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="missing bearer token")
    return token


def _validator(request: Request) -> TokenValidator:
    validator: TokenValidator | None = getattr(request.app.state, "token_validator", None)
    if validator is None:
        raise RuntimeError("install_token_validator() was not called at startup")
    return validator


def Internal(required_scope: str) -> Callable[[Request], Principal]:
    """A service-to-service route (ADR-0009 §2).

    Accepts only a client-credentials token (``aud=scaleforge-internal``)
    carrying ``required_scope`` in its space-delimited ``scope`` claim.
    Service subjects are never modelled in Casbin.
    """

    def dependency(request: Request) -> Principal:
        try:
            principal = _validator(request).validate(
                _bearer_token(request), audience=INTERNAL_AUDIENCE
            )
        except TokenValidationError as exc:
            raise HTTPException(status_code=401, detail="invalid token") from exc

        if required_scope not in principal.scopes():
            raise HTTPException(status_code=403, detail="insufficient scope")
        return principal

    dependency.sf_authz_marker = "internal"  # type: ignore[attr-defined]
    return dependency


def AuthenticatedUser() -> Callable[[Request], Principal]:
    """A user-context route carrying the forwarded Keycloak access token.

    Proves only that the caller is an authenticated user. Routes that expose
    only the caller's own data stay correct because repositories filter by
    ``sf_uid`` (LLD-02 §5 ownership rules) until ``Authorize(...)`` exists.
    """

    def dependency(request: Request) -> Principal:
        try:
            return _validator(request).validate(_bearer_token(request), audience=USER_AUDIENCE)
        except TokenValidationError as exc:
            raise HTTPException(status_code=401, detail="invalid token") from exc

    dependency.sf_authz_marker = "authenticated_user"  # type: ignore[attr-defined]
    return dependency


def Public(rate_class: str) -> Callable[[], None]:
    """An unauthenticated route.

    ``rate_class`` feeds the rate limiter (LLD-09 §7, not yet built).
    """

    def dependency() -> None:
        return None

    dependency.sf_authz_marker = "public"  # type: ignore[attr-defined]
    dependency.sf_authz_rate_class = rate_class  # type: ignore[attr-defined]
    return dependency
