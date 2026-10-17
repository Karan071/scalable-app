# ScaleForge

**ScaleForge is a design-stage, full-stack, multi-tenant commerce platform.** Its purpose is to show how a working storefront can be designed to remain secure, observable, and reliable as it grows into a distributed system. The repository currently contains the product requirements and architecture documents; application code has not been implemented yet.

## Product at a glance

Customers browse stores, search products, manage carts, check out, and track orders. Each merchant organization operates one tenant store. Organization staff manage that store's catalog, inventory, orders, and memberships; platform administrators manage users and organizations across the platform.

The design prioritizes correct commerce workflows, tenant isolation, explicit failure handling, and measured performance. Infrastructure is introduced in stages, and performance targets are hypotheses until validated by repeatable tests.

## Architecture

The planned system is a monorepo of a React single-page app, a FastAPI backend-for-frontend (BFF), and domain-oriented services. The browser talks to the BFF over the same origin. Services own their data and expose REST APIs; they coordinate through HTTP when a response is needed and RabbitMQ events for asynchronous work. Services may begin with separate schemas and roles on one PostgreSQL cluster; they never write to another service's tables.

| Area | Planned technology | Purpose |
| --- | --- | --- |
| Web app | React, TypeScript, Vite, React Router, TanStack Query | Storefront, customer account, organization console, platform admin, operations console |
| BFF and services | Python, FastAPI, Pydantic | Browser session boundary, API composition, and independently owned business domains |
| Identity | Keycloak, OpenID Connect (OIDC), Authorization Code + PKCE | Login, MFA, identity lifecycle; browser receives an opaque session cookie, not tokens |
| Authorization | Casbin, domain-aware RBAC | Default-deny permission checks scoped to organization/resource; enforced by backend services |
| Primary data | PostgreSQL | Authoritative business data, separate service schemas/roles, relational constraints and transactions |
| Shared state | Redis | Server-side sessions, caching, rate limits, and policy-change notifications |
| Search | PostgreSQL full-text search and `pg_trgm` | Derived product index; consider a dedicated search engine only if measurements justify it |
| Messaging | RabbitMQ, transactional outbox/inbox | Durable events, retries, dead-letter queues, and decoupled background work |
| Payments | Mock payment provider | Safe authorize/capture/webhook flows with synthetic data; no live payment credentials |
| Edge and runtime | Envoy Gateway, Docker, Kubernetes, Kustomize | TLS, routing, limits, containers, and staged deployment |
| Telemetry | OpenTelemetry SDKs and Collector; Prometheus, Tempo, Loki, Grafana | Correlated metrics, traces, logs, dashboards, and alerts |
| Quality | Pytest, Vitest, Playwright, k6, GitHub Actions | Backend/frontend checks, end-to-end flows, load experiments, and CI |

These are **planned choices**, recorded in draft ADRs and designs, not a description of installed dependencies or a running deployment.

## Core concepts

- **Multi-tenancy:** an organization is a merchant tenant and owns its store data. Customer accounts are platform-level and can shop at multiple stores. Carts are per customer and store; each order belongs to one store.
- **Authentication vs. authorization:** Keycloak verifies identity. The BFF keeps tokens server-side in Redis-backed sessions. Casbin and service-side ownership checks decide what that identity may do. Hiding a UI action is never a security check.
- **Service ownership:** each domain service owns its schema, rules, and API/event contracts. The initial logical boundaries include users, organizations, authorization, catalog, search, cart, inventory, orders, payments, notifications, and audit.
- **Sagas and compensation:** checkout crosses orders, inventory, and payments, so it cannot be one database transaction. The orders service coordinates a persisted state machine; failed steps trigger explicit actions such as releasing a reservation or voiding a payment authorization.
- **Reliable events:** a transactional outbox records an event in the same transaction as a business change, then publishes it. Consumers handle duplicate delivery idempotently; retries are bounded and repeated failures go to a dead-letter queue.
- **Correctness before optimization:** PostgreSQL is authoritative. Redis caches are disposable and have explicit expiry/invalidation rules. Search is a rebuildable projection. Bloom filters or separate search infrastructure are only retained when experiments show a real benefit.
- **Observability and resilience:** OpenTelemetry context follows requests and messages. Structured logs, metrics, traces, health checks, timeouts, bounded retries, failure exercises, and restore drills make system behavior diagnosable.
- **Security by boundary:** browser, edge, BFF, services, data stores, and identity provider have distinct responsibilities. Tenant checks, least-privilege service identities, CSRF protections, secret handling, and audit events are designed at the layer that can enforce them.

## Main workflows

**Login:** browser → BFF → Keycloak OIDC → opaque secure session cookie. The BFF stores tokens server-side; unsafe browser requests include a CSRF token.

**Checkout:** cart snapshot and price quote → inventory reservation → payment authorization → order confirmation. Follow-up work (inventory commit, payment capture, cart clear, notifications) runs asynchronously. Idempotency keys, webhooks, compensation, and reconciliation handle retries and partial failures.

**Catalog search:** catalog changes commit to PostgreSQL and publish through the outbox. Events update the search service's derived index; a reindex job can rebuild it from the catalog source.

## Repository guide

- [Product requirements](PRD.md) — product scope, requirements, security, reliability, and acceptance criteria.
- [Documentation map](docs/README.md) — reading order and the Low-Level Design (LLD) index.
- [Delivery plan](docs/00-delivery-plan.md) — milestones, requirements traceability, open questions, and review gates.
- [High-Level Design](docs/architecture/hld.md) — system boundaries, services, data ownership, trust boundaries, and deployment topology.
- [Architecture Decision Records](docs/adr/README.md) — decisions, alternatives, consequences, and revisit triggers.
- [Low-Level Designs](docs/architecture/lld/) — detailed designs for identity, authorization, commerce, messaging, data, observability, security, deployment, reliability, testing, and frontend.

## Delivery sequence

The plan progresses from requirements and architecture (M1), to identity and tenancy (M2), commerce (M3), performance and distributed workflows (M4), Kubernetes deployment (M5), then observability, reliability, and recovery evidence (M6). Designs are reviewed before the milestone that implements them. Architecture docs use `Draft`, `In review`, `Accepted`, and `Superseded` status; verify each document's status before treating a proposal as settled.

## Current status

This repository is currently a **planning and design artifact**. There is no application scaffold or runnable local setup yet. The architecture contains assumptions and open product questions, and its ADRs/LLDs are drafts; consult their status fields and the delivery plan before implementation.
