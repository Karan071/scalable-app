# ADR-0015: API conventions

- **Status:** Accepted (2026-10-17)
- **Date:** 2026-10-17

## Decision

| Concern | Convention |
| --- | --- |
| Style | Resource-oriented REST/JSON over HTTPS. Versioned path prefix `/v1`. Actions that don't fit CRUD use `POST /resource/{id}:action` (for example `:cancel`). |
| Contracts | OpenAPI 3.1 per service in `contracts/openapi/<service>.yaml`, generated from FastAPI and checked in. CI fails on breaking diffs without a version bump. |
| Tenant scoping | Org-scoped resources live under `/v1/orgs/{orgId}/…`. Platform resources under `/v1/platform/…`. Customer resources under `/v1/me/…`. Public storefront under `/v1/stores/{orgId}/…`. |
| IDs | UUIDv7 (time-ordered; index-friendly) for all entities. Never sequential integers in URLs. |
| Errors | RFC 9457 `application/problem+json` with `type` (stable URI), `title`, `status`, `detail` (safe), `instance`, `correlation_id`, and optional `errors[]` for validation. No stack traces or SQL in responses. |
| Pagination | Cursor (keyset) by default: `?limit=` (max 100) & `?cursor=`. Response `{items, next_cursor}`. The cursor is an opaque base64url string of the sort key tuple, HMAC-signed to prevent tampering. Offset paging only for relevance-ranked search, capped at page depth 50. |
| Concurrency | `ETag` (entity version) on GET. `If-Match` required on PATCH/PUT/DELETE of admin-edited entities. 412 on mismatch; 428 if the header is missing. |
| Idempotency | `Idempotency-Key` (UUID) required on checkout, payment, and other effectful POSTs listed in the LLDs. Same key + same payload → replayed response. Same key + different payload → 422. In progress → 409 + `Retry-After`. Keys retained for 24 h. |
| Rate limits | 429 with `Retry-After` and `RateLimit-Policy` / `RateLimit` headers (IETF httpapi draft) |
| Correlation | Accept/generate `X-Correlation-Id` at the BFF and propagate it. W3C `traceparent`/`tracestate` everywhere. |
| Validation | Pydantic models with `extra="forbid"`, explicit max lengths, enums, numeric bounds. Request-body limit 1 MiB by default. |
| Time | RFC 3339 UTC timestamps. Server clocks are authoritative; DB `now()` for business deadlines. |
| Money | Integer minor units + ISO 4217 currency code. Never floats. |
