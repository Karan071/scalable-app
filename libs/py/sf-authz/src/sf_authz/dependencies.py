"""FastAPI auth dependencies: ``Internal``, ``AuthenticatedUser``, ``Public``, ``Authorize``
(LLD-02 §6).
"""

import logging
import time
from collections.abc import Callable

from fastapi import FastAPI, HTTPException, Request

from .enforcer import PolicyEnforcer
from .tokens import Principal, TokenValidationError, TokenValidator

USER_AUDIENCE = "scaleforge-api"
INTERNAL_AUDIENCE = "scaleforge-internal"

#: Methods that count as "mutating" for the §8 staleness rule.
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

_deny_log = logging.getLogger("sf_authz.audit")
_deny_counts: dict[str, list[float]] = {}


def install_token_validator(app: FastAPI, validator: TokenValidator) -> None:
    app.state.token_validator = validator


def install_enforcer(app: FastAPI, enforcer: PolicyEnforcer) -> None:
    app.state.authz_enforcer = enforcer


def _enforcer(request: Request) -> PolicyEnforcer:
    enforcer: PolicyEnforcer | None = getattr(request.app.state, "authz_enforcer", None)
    if enforcer is None:
        raise RuntimeError("install_enforcer() was not called at startup")
    return enforcer


def _audit_deny(*, subject: str, domain: str, resource: str, action: str) -> None:
    """Sampled to at most 10/min per subject, always logged for platform-domain (LLD-02 §14).

    No subject ID in the logged fields — it's only the in-process dict key.
    """
    now = time.monotonic()
    bucket = _deny_counts.setdefault(subject, [])
    bucket[:] = [t for t in bucket if now - t < 60.0]
    if len(bucket) >= 10 and domain != "platform":
        return
    bucket.append(now)
    _deny_log.warning(
        "authz_deny", extra={"domain": domain, "resource": resource, "action": action}
    )


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
    dependency.sf_authz_scope = required_scope  # type: ignore[attr-defined]
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


def org_domain_from_path(param: str = "org_id") -> Callable[[Request], str]:
    """The ``org:<id>`` domain, read from the URL path parameter ``param``."""

    def resolve(request: Request) -> str:
        return f"org:{request.path_params[param]}"

    # Describes the resolver in route metadata (route_coverage.py).
    resolve.sf_authz_domain = f"org:{{{param}}}"  # type: ignore[attr-defined]
    return resolve


def Authorize(
    resource: str,
    action: str,
    *,
    domain: str | Callable[[Request], str],
    min_acr: int | None = None,
    max_auth_age_s: int | None = None,
) -> Callable[[Request], Principal]:
    """Full Casbin enforcement for a user-context route (LLD-02 §6 flowchart).

    ``domain`` is a fixed string (``"platform"``, ``"customer"``) or a
    resolver reading the URL path (``org_domain_from_path()``) — never
    headers, the body, or the session (FR-ORG-05).
    """

    def dependency(request: Request) -> Principal:
        # Bearer JWT valid? -> Principal
        try:
            principal = _validator(request).validate(_bearer_token(request), audience=USER_AUDIENCE)
        except TokenValidationError as exc:
            raise HTTPException(status_code=401, detail="invalid token") from exc

        # Resolve domain from the path/fixed string only.
        dom = domain(request) if callable(domain) else domain

        enforcer = _enforcer(request)

        # Domain active?
        if enforcer.domain_status(dom) in ("suspended", "archived"):
            raise HTTPException(status_code=403, detail="org_suspended")

        # Policy fresh?
        staleness = enforcer.staleness_s()
        if staleness > 300.0:
            raise HTTPException(status_code=503, detail="authz_stale")
        if staleness > 60.0 and (request.method in MUTATING_METHODS or dom == "platform"):
            raise HTTPException(status_code=503, detail="authz_stale")

        # acr / auth age meets route + platform rules?
        effective_min_acr = max(min_acr or 0, 2 if dom == "platform" else 0)
        acr = int(principal.claim("acr") or 0)
        if effective_min_acr and acr < effective_min_acr:
            raise HTTPException(status_code=401, detail="step_up_required")
        if max_auth_age_s is not None:
            auth_time = principal.claim("auth_time")
            if auth_time is None or (time.time() - float(auth_time)) > max_auth_age_s:
                raise HTTPException(status_code=401, detail="step_up_required")

        # Enforce sub, dom, obj, act.
        try:
            subject = principal.casbin_subject()
        except TokenValidationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

        if not enforcer.enforce(subject, dom, resource, action):
            _audit_deny(subject=subject, domain=dom, resource=resource, action=action)
            raise HTTPException(status_code=403, detail="forbidden")

        return principal

    dependency.sf_authz_marker = "authorize"  # type: ignore[attr-defined]
    dependency.sf_authz_resource = resource  # type: ignore[attr-defined]
    dependency.sf_authz_action = action  # type: ignore[attr-defined]
    dependency.sf_authz_domain = (  # type: ignore[attr-defined]
        domain if isinstance(domain, str) else getattr(domain, "sf_authz_domain", "dynamic")
    )
    dependency.sf_authz_min_acr = min_acr  # type: ignore[attr-defined]
    return dependency
