# LLD-11: Security Architecture, Threat Model, and Controls

| Field | Value |
| --- | --- |
| Status | Draft v0.1 (threat model is updated each milestone) |
| Milestone | M1 (threat model v1) → all |
| ADRs | 0009, 0010, 0012 |

## 1. Scope

This LLD covers:

- the system threat model;
- the mapping of PRD controls SEC-01..SEC-15 to designs and tests;
- web application security;
- service identity and secrets;
- encryption and key management;
- edge protections;
- the security testing pipeline.

The authorization-specific threat model is in LLD-02 §10.

## 2. Threat model (STRIDE by trust boundary)

Trust zones are defined in HLD §6.

| Boundary | S (spoofing) | T (tampering) | R (repudiation) | I (info disclosure) | D (denial of service) | E (elevation) |
| --- | --- | --- | --- | --- | --- | --- |
| Browser → Edge/BFF | Session theft or fixation → `__Host-` HttpOnly cookie, rotation, binding cookie at login (LLD-01) | Request tampering → server-side totals, schema `extra=forbid` | Users deny actions → audit with session ref | Token leakage → tokens never leave the BFF; XSS → CSP | Floods → edge + BFF rate limits; size limits | CSRF → token + Origin; XSS → CSP, sanitization |
| BFF → Services | Forged identity headers → JWT only, headers ignored | Path/org confusion → domain from path + resource org check | Service actions untraceable → `on_behalf_of` in audit | Over-broad responses → explicit response models | Retry storms → budgets, breakers | Confused deputy → user token for user calls (ADR-0009) |
| Service → Service | Rogue pod → client-credential tokens, NetworkPolicy allow-lists | Message tampering → broker ACLs per service; schema validation | Event provenance → `source` + `causationid` | Payload leakage → no secrets in events (one encrypted exception) | Poison messages → delivery limit + DLQ | Over-privileged service tokens → per-operation scopes |
| Service → Data | Credential reuse → per-service DB and Redis users | SQL injection → ORM/parameterized; cross-schema writes → grants | DB changes untraceable → migrations in Git; audit for business changes | Backup theft → encrypted backups; tokens encrypted in Redis | Slow queries → statement timeouts, pool limits | Superuser DB access → none for services |
| Provider → Webhooks | Forged callbacks → HMAC signature | Replay → timestamp window + event-ID dedupe | — | — | Webhook floods → per-route rate limit, small bodies | — |
| Admins → Keycloak / ops | Phished admin → MFA + step-up; WebAuthn recommended | Realm drift → realm-as-code; admin events | — | Admin console exposure → no public route | — | Admin-API misuse → narrowly scoped service accounts |
| Telemetry | — | — | — | Secrets in traces/logs → redaction (LLD-10 §6) | Cardinality bombs → guards | Grafana access → OIDC + RBAC |
| Supply chain | Malicious dependency → pinned hashes, scanning | Image tampering → digests, signed images (cosign, M5) | Build provenance → SLSA-style attestations (optional) | Secrets in repo → gitleaks | — | — |

## 3. Control mapping (SEC-01..SEC-15)

| ID | Control | Design | Verification (LLD-14 §4) |
| --- | --- | --- | --- |
| SEC-01 | Authentication | BFF session required on protected routes; services require a valid JWT on every non-`Public` route; route-coverage guard | Unauthenticated sweep over all routes from OpenAPI → 401 |
| SEC-02 | Authorization | Casbin default deny in every service (LLD-02) | Decision table + matrix tests |
| SEC-03 | Tenant isolation | Domain from path, `TenantScope` repositories, 404 on foreign IDs, optional RLS | Cross-tenant matrix, IDOR fuzzing |
| SEC-04 | CSRF | Synchronizer token + Origin/Sec-Fetch-Site + SameSite=Lax + JSON-only (LLD-01 §7) | CSRF test suite + ZAP |
| SEC-05 | XSS | React escaping by default; no `dangerouslySetInnerHTML` except sanitized product HTML; strict CSP (§4.2); Trusted Types (evaluate) | XSS payload corpus E2E; CSP report monitoring |
| SEC-06 | SQL injection | SQLAlchemy Core/ORM with bound parameters; `websearch_to_tsquery` with a bound parameter; no string-built SQL (lint rule bans `text()` with f-strings) | Bandit/Semgrep rules; sqlmap against staging search/filter endpoints |
| SEC-07 | Transport | TLS at the gateway (HSTS 1 year, TLS 1.2+); TLS to PostgreSQL, Redis, and RabbitMQ in staging; mTLS deferred (§5) | testssl.sh against the gateway; config checks |
| SEC-08 | Secrets | No secrets in Git (gitleaks); Kubernetes Secrets injected from External Secrets Operator ← OpenBao in staging; `.env` (git-ignored) locally; rotation runbooks | CI gitleaks; secret inventory review |
| SEC-09 | Encryption | Disk/volume encryption per environment; encrypted backups; field-level AES-GCM for tokens in Redis and the invitation token in the outbox; key management (§6) | Backup restore test validates encryption; key rotation drill |
| SEC-10 | Audit | LLD-04 §2: outbox-backed, append-only, hash chain, allow-listed fields | Audit emission tests |
| SEC-11 | API protection | Rate limits (LLD-09 §7), Pydantic validation with bounds, body size limits (edge + app), RFC 9457 safe errors, no stack traces | API negative tests; fuzzing (Schemathesis) |
| SEC-12 | Service identity | Per-service Keycloak clients and scopes; per-service DB, Redis, and RabbitMQ users; Kubernetes ServiceAccounts per workload | Config tests; NetworkPolicy tests |
| SEC-13 | JWT validation | `sf-authz` validates sig (JWKS), iss, aud, exp/nbf, alg allow-list, required claims; back-channel logout tokens validated (LLD-01 §8) | Unit tests with forged, expired, wrong-aud, `alg=none`, and HS256-confusion tokens |
| SEC-14 | Supply chain | CI: pip-audit, npm audit, Semgrep/Bandit, Trivy image scan, gitleaks, SBOM (Syft); fail on critical/high with a waiver process; pinned base images by digest; Renovate for updates | CI gates (LLD-12 §12) |
| SEC-15 | Admin access | Platform roles require MFA (`acr ≥ 2`) and step-up for privileged operations; `X-Access-Reason` for tenant access; Keycloak admin console not public; least-privilege admin service accounts | Step-up E2E; config review |

## 4. Web application security

### 4.1 Response headers (set at the BFF for app and API responses; static assets via the gateway)

| Header | Value |
| --- | --- |
| `Strict-Transport-Security` | `max-age=31536000; includeSubDomains` |
| `Content-Security-Policy` | §4.2 |
| `X-Content-Type-Options` | `nosniff` |
| `Referrer-Policy` | `strict-origin-when-cross-origin` |
| `Permissions-Policy` | `camera=(), microphone=(), geolocation=(), payment=()` |
| `Cross-Origin-Opener-Policy` | `same-origin` |
| `Cross-Origin-Resource-Policy` | `same-origin` (API), `same-site` (media) |
| `Cache-Control` | `no-store` on `/auth/*` and authenticated API responses |

### 4.2 Content Security Policy

```text
default-src 'self';
script-src 'self';
style-src 'self';
img-src 'self' data: blob:;
connect-src 'self';
font-src 'self';
frame-ancestors 'none';
form-action 'self' https://<keycloak-host>;
base-uri 'self';
object-src 'none';
upgrade-insecure-requests;
report-to csp
```

- Vite builds produce hashed assets with no inline scripts.
- Starts in `Content-Security-Policy-Report-Only` in M2 and becomes enforcing by M3.
- Reports go to the BFF `/api/csp-report`, which is rate-limited.

### 4.3 Input and output handling

- **Validation:** all input is validated by Pydantic models with explicit bounds. Unknown fields are rejected.
- **Untrusted rich text:** only product descriptions. They are sanitized server-side (allow-list) **and** client-side (DOMPurify) before rendering.
- **Uploads:** they never pass through app servers (presigned uploads); magic-byte checks and re-encoding apply (LLD-05 §5).
- **Errors:** problem details with a stable `type`, a safe `detail`, and `correlation_id`. Internal details go only to logs.
- **Open redirects:** `return_to` is validated against an allow-list of relative prefixes (LLD-01 §4).

## 5. Service identity, secrets, and IAM

| Identity | Mechanism |
| --- | --- |
| Workload → Kubernetes API | Dedicated ServiceAccount; `automountServiceAccountToken: false` unless needed (KEDA, CNPG) |
| Service → service | Keycloak client-credentials token, audience `scaleforge-internal`, per-operation scopes; NetworkPolicy allow-list |
| Service → PostgreSQL / Redis / RabbitMQ | Per-service credentials from Secrets; least-privilege grants and ACLs |
| Humans → cluster | Kubernetes RBAC: `viewer`, `operator` (exec only in non-prod), `admin`; kubeconfigs not shared |

- **Secrets flow:**
  - **Local:** a git-ignored `.env` → Compose, or a script creates Kubernetes Secrets.
  - **Staging:** OpenBao (Vault-compatible) → External Secrets Operator → Kubernetes Secrets → mounted files (not environment variables, where feasible).
- **Rotation:**
  - DB passwords are rotated by CNPG-managed secrets with app restart.
  - Webhook secrets use dual-secret verification.
  - Session encryption keys use a keyring (LLD-01 §5.5).
  - Keycloak client secrets: generate new → deploy → revoke old.
- **mTLS:** deferred. Revisit if a service mesh is adopted or the threat model requires workload authentication beyond tokens and NetworkPolicies (PRD §9.1 "where justified").

## 6. Encryption and key management

| Data | At rest | In transit |
| --- | --- | --- |
| PostgreSQL data | Volume encryption (provider or LUKS on the staging VM); backups encrypted by object-storage SSE + Barman encryption | TLS |
| Session tokens in Redis | AES-256-GCM field encryption (keyring) + AOF on an encrypted volume | TLS (staging) |
| Invitation token in outbox and event | AES-256-GCM (orgs key; notifications holds decrypt) | TLS |
| Backups | Encrypted; access limited to the backup role; restore drill verifies decryptability | TLS |
| Object storage (images) | SSE | TLS |

- **Key management:** application data keys live in OpenBao (staging) and are delivered as Secrets.
- **Rotation cadence:** session keys every 90 days; webhook secrets every 90 days; DB credentials every 30–90 days.
- **Key inventory:** `docs/security/key-inventory.md` (planned) records owner, rotation cadence, and blast radius for each key.

## 7. Edge protection: WAF and DDoS

- **Local and kind:** Envoy Gateway global rate limits and size limits. These **do not** constitute DDoS protection (PRD §8).
- **WAF:** Coraza with the OWASP Core Rule Set as an Envoy extension, evaluated in M6. Starts in detection mode, then blocking for high-confidence rules. False positives are tracked.
- **Volumetric DDoS:** only verifiable with a provider edge (CDN/L4 protection) in a cloud staging environment. Reports must state which protections were actually present.

## 8. Security testing and CI gates

| Stage | Tooling | Gate |
| --- | --- | --- |
| Pre-commit | gitleaks, ruff (incl. bandit rules), eslint (security plugin) | Block on findings |
| CI (per PR) | Semgrep (Python, TS rulesets + custom rules: no raw SQL f-strings, no `dangerouslySetInnerHTML` outside the sanitizer component, no header-derived identity), pip-audit, npm audit, Trivy (images + IaC config), Syft SBOM, Squawk (migrations) | Critical/high fail; waivers require an expiry date and an owner |
| CI (integration) | Security test suites: authn sweep, authz/tenant matrix, CSRF, session, JWT forgery, rate limits, webhook signature | Must pass |
| Nightly (staging) | OWASP ZAP baseline + authenticated API scan (from OpenAPI), Schemathesis fuzzing, testssl.sh | Findings triaged within 5 working days |
| Per milestone | Threat model review; dependency and secret inventory review | Signed off in the milestone review |

## 9. Requirements covered

PRD §8 (SEC-01..15); PRD §2.5 (privileged controls); §3.3 (authorization security); §7.3 (telemetry privacy); Appendix A Security group.

## 10. Open items

| Item | Owner |
| --- | --- |
| Trusted Types feasibility with chosen libraries | Frontend |
| cosign signing + admission policy (Kyverno) in M5 | Platform |
| Cloud staging for WAF/DDoS verification (OQ-7) | Product/Platform |
