# ADR-0012: Edge — Kubernetes Gateway API with Envoy Gateway

- **Status:** Proposed
- **Date:** 2026-10-17

## Context

PRD §4.1 leaves the ingress implementation open (Ingress or Gateway). Upstream ingress-nginx has been retired. Its maintenance ended in March 2026, so it should not be used for new work. The edge must provide:

- TLS;
- routing;
- request-size limits;
- per-IP rate limits;
- traffic splitting for canary and blue-green releases (PRD §9.3);
- WAF integration in the target environment.

## Decision

- Use the **Gateway API** (`Gateway`, `HTTPRoute`) implemented by **Envoy Gateway**.
- **Gateway API resources:**
  - `HTTPRoute` weights for canary;
  - route switching for blue-green.
- **Envoy Gateway extension policies:**
  - `ClientTrafficPolicy` for size limits and timeouts;
  - `BackendTrafficPolicy` for per-IP global rate limits (backed by redis-cache) and retries off by default;
  - `SecurityPolicy` for CORS (not needed for same-origin) and IP allow-lists on admin routes.
- **TLS:** cert-manager issues certificates (a local CA in kind; ACME in a real environment).
- **WAF:** Coraza (OWASP CRS) as an Envoy extension, evaluated in M6. It is verified only where deployed (PRD §8).

## Alternatives

| Option | Why not |
| --- | --- |
| ingress-nginx | Retired upstream |
| Traefik / HAProxy / Kong / Istio ingress | All viable. Envoy Gateway is the Gateway API reference-style implementation with built-in global rate limiting. Istio adds a mesh we do not need yet. |

## Consequences

- Edge configuration is Gateway API CRDs, portable across implementations except for the extension policies.
- Envoy Gateway's global rate limiting needs Redis access from the edge namespace.
