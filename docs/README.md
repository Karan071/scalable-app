# ScaleForge Architecture Documentation

| Field | Value |
| --- | --- |
| Status | Accepted v1.0 — M1 closed; M2 (Identity and multi-tenancy) in progress |
| Source | [PRD.md](../PRD.md) v1.0 (Draft for review) |
| Last updated | 2026-10-17 |
| Scope | M1 design is accepted. M2 implementation is starting; see [delivery plan](00-delivery-plan.md). |

This folder turns the PRD into a reviewable architecture. The work runs in three layers:

1. **Delivery plan**: how and in what order the PRD becomes a running system, with a traceability matrix from PRD IDs to design sections, milestones, and tests.
2. **High-Level Design (HLD)**: system boundary, services, data ownership, trust boundaries, communication styles, and deployment topology.
3. **Low-Level Designs (LLDs)**: one per domain or cross-cutting concern, with data models, APIs, state machines, algorithms, failure handling, telemetry, and tests.

Architecture Decision Records (ADRs) settle the PRD's open decisions (PRD §13.1) and any other choice with real alternatives.

## Reading order

| # | Document | Purpose |
| --- | --- | --- |
| 0 | [Delivery plan](00-delivery-plan.md) | Phases, milestone work breakdown, traceability, open questions |
| 1 | [HLD](architecture/hld.md) | Target architecture at system level |
| 2 | [ADR index](adr/README.md) | Decisions, alternatives, consequences |
| 3 | LLDs (below) | Detailed design per area, in milestone order |

## LLD map

| LLD | Area | Needed by |
| --- | --- | --- |
| [LLD-01](architecture/lld/01-identity-bff-sessions.md) | Keycloak, BFF, OIDC, sessions, CSRF, MFA, user lifecycle | M2 |
| [LLD-02](architecture/lld/02-authorization-casbin.md) | Casbin model, enforcement, policy propagation, authz threat model | M2 |
| [LLD-03](architecture/lld/03-organizations-membership.md) | Organizations, memberships, invitations, org switching | M2 |
| [LLD-04](architecture/lld/04-audit-notifications.md) | Audit trail, notifications | M2 (audit), M4 (notifications) |
| [LLD-05](architecture/lld/05-catalog-search.md) | Catalog, pricing, images, search index | M3 |
| [LLD-06](architecture/lld/06-cart-checkout-orders-payments.md) | Cart, checkout saga, orders, payments, mock provider | M3 → M4 |
| [LLD-07](architecture/lld/07-inventory.md) | Stock, reservations, concurrency, expiry | M3 |
| [LLD-08](architecture/lld/08-messaging-events.md) | RabbitMQ topology, outbox/inbox, event catalog, retry/DLQ | M2 (minimal) → M4 |
| [LLD-09](architecture/lld/09-data-caching-ratelimiting.md) | PostgreSQL topology, query rules, caching, rate limiting, experiments | M3 → M4 |
| [LLD-10](architecture/lld/10-observability.md) | OTel, Collector, metrics/logs/traces, dashboards, alerts, SLOs | M2 (baseline) → M6 |
| [LLD-11](architecture/lld/11-security.md) | Threat model, control mapping SEC-01..15, secrets, supply chain | M1 → all |
| [LLD-12](architecture/lld/12-platform-deployment.md) | Repo, Docker, Kubernetes, Gateway, probes, release, CI/CD | M5 |
| [LLD-13](architecture/lld/13-reliability-dr.md) | Resilience parameters, failure lab, backup/restore, clock skew | M4 → M6 |
| [LLD-14](architecture/lld/14-testing-strategy.md) | Test categories, tools, load/resilience plans, acceptance | All |
| [LLD-15](architecture/lld/15-frontend.md) | React app areas, BFF integration, debouncing, XSS/CSP | M2 → M3 |

## Conventions

- **PRD IDs** (`FR-*`, `SEC-*`) are cited in brackets, for example [FR-CHK-03].
- **IDs introduced here:**
  - `ASM-n`: assumption (HLD §2)
  - `OQ-n`: open question for the product owner (delivery plan §5)
  - `ADR-nnnn`: decision record
  - `FS-n`: failure scenario (LLD-13)
  - `EXP-n`: performance experiment (LLD-09)
- **Status values:** `Draft` → `In review` → `Accepted` → `Superseded by …`.
- **Normative words:** *must* or *must not* marks a constraint that reviews check. *Should* marks a default that you can override with a documented reason.
- **Diagrams** are Mermaid, so they render in GitHub and the VS Code preview. Each diagram is the source of truth for the structure it shows. If the diagram and the prose disagree, fix the diagram.
- **LLD ending:** every LLD ends with *Requirements covered*, *Telemetry*, *Tests*, and *Open items*.
- **Numbers** (timeouts, TTLs, limits) are initial values. Load tests calibrate them in M4/M6 (PRD §7.4, §10). They are not performance claims.

## Change process

1. Changes to these docs go through pull-request review.
2. A change that alters service boundaries, data ownership, a trust boundary, or a technology choice needs a new ADR or a superseding ADR.
3. An LLD moves to `Accepted` when it passes the LLD review checklist ([delivery plan §6](00-delivery-plan.md#6-lld-review-checklist)).
