# LLD-14: Testing Strategy and Release Acceptance

| Field | Value |
| --- | --- |
| Status | Draft v0.1 (sections grow each milestone) |
| Milestone | M2 → M6 |
| Related | PRD §11, LLD-11 §8, LLD-12 §12, LLD-13 §3 |

## 1. Test layers and ownership

| Category (PRD §11) | Scope | Owner | Runs |
| --- | --- | --- | --- |
| Unit | Business rules, validation, pricing, state machines, authz decisions | Service team | Every PR |
| Integration | PostgreSQL transactions, Redis sessions, Keycloak OIDC, Casbin enforcement, RabbitMQ outbox/inbox | Service team | Every PR (affected services) |
| API | Authn, authz, validation, pagination, errors, rate limits; contract conformance | Service team | Every PR |
| End-to-end | Login, browse/search, cart, checkout, orders, org admin, platform admin | Frontend + backend | PR subset; full nightly |
| Concurrency | Reservations, duplicate checkout, concurrent edits, locking | Service team | Every PR (fast set), nightly (scaled) |
| Performance | Search, product reads, checkout, cache, DB saturation, p95/p99 | Perf owner | Nightly smoke; milestone runs |
| Resilience | FS catalog (LLD-13 §3) | Platform | Nightly (Compose subset); milestone (cluster) |
| Security | Tenant isolation, privilege escalation, CSRF, XSS, SQLi, session revocation | Security + teams | Every PR (suites), nightly (DAST) |
| Deployment | Readiness, rolling updates, rollback, resources, config errors | Platform | Ephemeral kind per PR (subset), nightly |
| Recovery | Backup restore, event replay, payment reconciliation, RPO/RTO | Platform | Milestone drills (M6+), weekly restore verification |

## 2. Tooling

| Purpose | Tools |
| --- | --- |
| Python tests | pytest, pytest-asyncio, Hypothesis (property tests: pricing, state machines, cursor encoding), respx (httpx mocking) |
| Real dependencies | Testcontainers (PostgreSQL, Redis, RabbitMQ, Keycloak, MinIO). **Interim (M2):** integration tests run against an isolated database (`scaleforge_test`), RabbitMQ vhost (`scaleforge-test`), and Redis DB index on the Compose stack, rebuilt from scratch by `make test-env` before every `make test`. Tests never share state with the dev environment. Still pending: Testcontainers for CI (E5.5), where no Compose stack runs. Keycloak is faked in service tests. |
| Contract | Schemathesis (OpenAPI-driven fuzzing and conformance), JSON Schema validation for events, OpenAPI diff |
| Frontend | Vitest + Testing Library, MSW for API mocks, axe for accessibility smoke |
| E2E | Playwright (Chromium + WebKit), TOTP helper for MFA flows |
| Load | k6 (arrival-rate executors), with the k6 Prometheus remote-write output to Grafana |
| Fault injection | Toxiproxy (Compose), Chaos Mesh (kind/staging), libfaketime/TimeChaos |
| Security | OWASP ZAP, sqlmap (staging only), Semgrep, Bandit, gitleaks, Trivy, testssl.sh |
| Observability tests | In-memory OTel exporters, the Jaeger/Tempo query API, `promtool test rules` |

## 3. Environments and test data

- **Synthetic data only.** A generator in `perf/datagen` creates:
  - organizations (with Zipf-distributed sizes);
  - products and SKUs (Zipf popularity), categories, and stock;
  - customers;
  - historical orders.

  It is seeded and deterministic (`--seed`), with volume profiles `small` (dev), `medium` (CI perf smoke), and `large` (staging: e.g. 200 orgs, 500k products, 1M users, 5M orders, to be finalized).
- **Keycloak test realm users** have known TOTP seeds (test realm only). The users are created by realm import for E2E.
- **Isolation:** each integration test runs in a transaction, or in a schema reset per module. E2E creates fresh orgs per test (tenant isolation keeps tests independent).

## 4. Security test suites

| Suite | Method | Pass criteria |
| --- | --- | --- |
| Authn sweep | For every route in every OpenAPI document (non-`Public`): call without a session/token → 401 | 100% of routes |
| Tenant-isolation matrix | Generated from route metadata (`Authorize(resource, action, domain)`). For each org-scoped route: actors {member with the right role in the other org, member without the role, non-member, customer, disabled membership, suspended org} × {own-org path, other-org path, other-org resource ID in own-org path} | Expected 403/404 for every cell. Zero 2xx. |
| Privilege escalation | LLD-02 §11 rules; mass-assignment attempts (extra fields like `role`, `org_id`, `status` in bodies) | All rejected |
| CSRF | Missing/invalid token; cross-origin `Origin`; form content types; `SameSite` behavior in Playwright with a cross-site page | Rejected |
| XSS | Payload corpus in product names/descriptions, display names, search queries; rendered in storefront and admin; CSP violation reports captured | No script execution; no CSP violations from app code |
| SQL injection | Semgrep rules + sqlmap on search/filter/sort params in staging | No injectable parameters |
| Session | Fixation, rotation on step-up, idle/absolute expiry, logout revocation across replicas, back-channel logout, disabled user | Per LLD-01 §16 |
| JWT | Forged signature, `alg=none`, HS/RS confusion, wrong `iss`/`aud`, expired, missing claims, unknown `kid` | All rejected |
| Token leakage | Scan every BFF response for JWT patterns and token field names; Playwright inspects browser storage and cookies (`document.cookie` must not contain the session) | None found |
| Rate limits | Each class: exceed → 429 + headers; consistent across replicas; XFF spoofing ignored | Pass |
| Webhooks | Invalid signature, replay, stale timestamp, oversize | Rejected, no state change |

## 5. Concurrency tests

The harness uses async workers released together by a barrier (`asyncio.Event`), N runs per test (default 20). It asserts **invariants via SQL afterwards**, not only responses.

| Test | Invariant |
| --- | --- |
| Oversell: stock S, N > S concurrent single-unit checkouts (distinct customers) | Confirmed lines = S; `reserved` + `on_hand` match the ledger |
| Duplicate checkout: same key ×50; two keys × same cart version | 1 order |
| Multi-SKU opposite-order reservations | No surfaced deadlock errors; invariants hold |
| Expiry vs commit race | Exactly one outcome; ledger consistent |
| Concurrent product edits with the same ETag | One success, others 412 |
| Invitation double-accept | 1 membership |
| Last-admin mutual removal | ≥ 1 admin remains |
| Token-refresh race (20 concurrent near expiry) | 1 refresh call |
| Cache stampede on a hot key | ≤ 2 DB loads |
| Policy reload during traffic | No 5xx; decisions flip within the target |

## 6. Performance testing

### 6.1 Workload model (hypothesis; calibrated in M4)

| Scenario | Share | Steps |
| --- | --- | --- |
| Browse | 60% | Store home → category listing → 2–4 product details (Zipf) |
| Search | 25% | Typed query (debounced) → results → product detail |
| Cart | 10% | Login (session reuse) → add/update/remove items → cart view |
| Checkout | 4% | Cart → checkout (success 90%, decline 7%, out-of-stock 3%) → order status |
| Admin | 1% | Product edit, stock adjust, order transition |

Think time is 3–8 s (lognormal). Sessions are pre-created for authenticated scenarios, so login load is tested separately.

### 6.2 k6 design

- **Open model** (`constant-arrival-rate` / `ramping-arrival-rate`), which avoids coordinated omission.
- One script per scenario plus a `mix` script.
- Thresholds encode the provisional SLOs, for example `http_req_duration{route:product_detail}: p(95)<250`.

### 6.3 Test types

| Type | Profile | Purpose |
| --- | --- | --- |
| Smoke | 5 min, low rate | Nightly regression |
| Baseline / load | 5 min warm-up + 30 min steady at the target rate | SLO validation |
| Step / knee | +X req/s every 5 min until errors > 1% or p95 > 2× SLO | Find capacity and the bottleneck |
| Soak | 2 h at 70% of the knee | Leaks, pool drift, queue growth |
| Spike | 5× for 2 min | Autoscaling, load shedding, recovery |
| Hot-SKU burst | EXP-9 | Contention |

### 6.4 Report template (`docs/reports/perf/<date>-<scenario>.md`)

1. **Question / hypothesis**.
2. **Environment:** cluster type and size, node specs, versions (git SHA, images), replica counts, resource settings, data volume profile, and the known differences from production. kind on a VM is **not** production-representative (ADR-0014).
3. **Workload:** script, arrival rates, mix, duration, warm-up.
4. **Baseline results:**
   - throughput;
   - error rate;
   - p50/p95/p99 per route;
   - DB metrics (QPS, CPU, pool wait, top queries);
   - cache hit rate;
   - queue depths;
   - pod resources;
   - cost proxies (CPU-seconds, GB-hours, telemetry volume).
5. **Change applied**.
6. **After results:** same metrics, as a delta table.
7. **Evidence:** dashboard snapshots, EXPLAIN plans, traces.
8. **Decision:** keep, reject, or follow up, with operational limits and rollback.
9. **Limitations** of the test.

## 7. Resilience tests

- These are the FS-1..FS-14 scenarios in LLD-13 §3.
- **Automated in Compose (nightly):** FS-1, FS-2(b), FS-3, FS-4, FS-5, FS-7, FS-8, FS-9, FS-10.
- **Cluster runs (M6):** all scenarios. Each run produces a pass/fail record with the observed metrics.

## 8. Deployment and recovery tests

| Test | Pass criteria |
| --- | --- |
| Rolling update under load | 0 failed requests; p95 within 20% |
| Rollback (`rollout undo` / digest revert) | Previous version serving in < 2 min; no schema rollback needed |
| Config error | New pods fail at startup; rollout halts; old pods serve |
| Resource constraint | OOM → restart observed; HPA scale-out within 2 min under CPU load |
| Readiness semantics | Optional dependency down → pods remain ready |
| Restore drill | LLD-13 §5.4: measured RPO/RTO, integrity checks pass |
| Outbox replay | Replay a window → no duplicate side effects; missing consumers backfilled |
| Payment reconciliation | Seeded mismatches resolved; rerun is a no-op |

## 9. Release acceptance checklist (PRD §11) → evidence

| PRD acceptance item | Evidence |
| --- | --- |
| Catalog, search, cart, checkout, orders end-to-end | Playwright full suite report (nightly, green ×3 consecutive) |
| Checkout idempotent; inventory correct under concurrency | §5 concurrency report |
| Business data persists across restarts | E2E: create data → restart all containers/pods → verify |
| Keycloak login/MFA via BFF; no tokens in the browser | LLD-01 §16 tests + token leakage suite |
| Redis sessions across BFF replicas; logout revokes | Multi-replica integration test |
| Casbin enforces tenant roles and platform privileges | Decision table + matrix suite |
| Automated tests reject cross-tenant access and unauthorized mutations | Tenant-isolation matrix (zero 2xx) |
| Service ownership, APIs, event contracts documented | HLD §5/§8.2, OpenAPI + event schemas in `contracts/`, CI contract gates |
| Async retry/duplicate/DLQ behavior defined | LLD-08 §6–§7 + FS-5 + DLQ tests |
| Docker/Kubernetes deployment repeatable | Ephemeral kind CI job + `make cluster-up && make deploy` from a clean machine (recorded) |
| OTel traces, metrics, logs available and correlated | Trace-continuity test + Grafana trace↔log links demo |
| Dashboards: latency, errors, throughput, dependency health | Provisioned dashboards (LLD-10 §8) |
| Load tests, failure simulations, backup restoration documented | `docs/reports/perf/`, `docs/reports/chaos/`, restore drill report |
| ADRs explain trade-offs; performance claims reproducible | `docs/adr/`; reports with environment + scripts + commit SHA |

## 10. CI matrix

| Trigger | Runs |
| --- | --- |
| PR | Lint, types, unit, contract, affected-service integration + API + security suites, image build + scans, ephemeral kind smoke + E2E subset |
| Main | All of the above for all services + sign/publish |
| Nightly | Full E2E, Compose resilience subset, ZAP baseline, Schemathesis, k6 smoke, restore verification (weekly) |
| Milestone | Full perf runs (§6.3), cluster failure lab, restore drill, acceptance checklist review |
