# ADR-0009: Service identity and user-context propagation

- **Status:** Accepted (2026-10-17)
- **Date:** 2026-10-17

## Context

Requirements:

- Services must enforce authorization themselves (PRD §3.3).
- Services use scoped identities (SEC-12) and validate JWTs fully (SEC-13).
- Tokens must never reach the browser (FR-AUTH-03). They may exist server-side.

## Decision

1. **User-context calls** (BFF → service, service → service within a user request):
   - The caller forwards the user's **Keycloak access token** as `Authorization: Bearer`.
   - Token properties:
     - audience `scaleforge-api`, lifetime 5 min;
     - claims `sub`, `sf_uid` (internal user UUID, via a protocol mapper from a user attribute set at provisioning), `acr`, `auth_time`, `sid`;
     - **no application roles** (Casbin decides).
2. **Service-originated calls** (consumers, jobs, reconciliation, `/internal/*` endpoints):
   - Client-credentials token per service (`sf-svc-<name>`), audience `scaleforge-internal`.
   - Scopes per internal operation (for example `inventory:reservations:write`).
   - Authorization for these calls is the scope check on `Internal(scope)` routes. Service subjects are not modelled in Casbin.
   - When a service acts for a user (e.g. a saga step), the user is carried as `on_behalf_of` for audit.
3. **Validation in every service** (`sf-authz`):
   - Signature via cached JWKS (refetch on unknown `kid`, at most 1 per minute).
   - `iss` exact match, `aud` contains the expected audience, `exp`/`nbf` with 30 s leeway, `typ`/`alg` allow-list (RS256/ES256 only).
   - Required claims present.
4. **Headers are never trusted:**
   - Identity and tenant are never taken from custom headers (`X-User-Id`, `X-Org-Id`).
   - The tenant comes from the URL path and is checked by Casbin.

## Alternatives

| Option | Why not (now) |
| --- | --- |
| BFF mints its own internal JWT | Second token issuer to secure and rotate. Loses the Keycloak `acr`/`auth_time` provenance. |
| OAuth token exchange (RFC 8693) per downstream audience | Better least privilege (down-scoped audiences), but one more Keycloak round-trip per service. Revisit for high-privilege services. |
| mTLS-only service identity | Proves the workload but not the user. Considered as an addition (PRD §9.1). |

## Consequences

- The user's token is valid at every service that accepts `scaleforge-api` for its 5-minute lifetime. Mitigations:
  - short TTL;
  - NetworkPolicies, so services are reachable only from the BFF and allowed peers;
  - token exchange as a later hardening step.
- The `sf_uid` claim requires a one-time token refresh after first-login provisioning (LLD-01 §4).
