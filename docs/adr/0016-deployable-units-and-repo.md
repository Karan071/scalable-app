# ADR-0016: Deployable units and repository layout

- **Status:** Accepted (2026-10-17)
- **Date:** 2026-10-17

## Context

The PRD lists 12 logical services plus a mock provider. It also warns about operational overhead and overengineering (§13.2).

## Decision

1. **Monorepo** with this top-level layout:

   ```
   apps/web/                 React + TS + Vite
   services/<name>/          one FastAPI app per logical service
   libs/py/                  sf-common, sf-authz, sf-messaging, sf-db
   contracts/openapi/        per-service OpenAPI (checked in)
   contracts/events/         JSON Schemas per event type and version
   deploy/compose/           Docker Compose files
   deploy/k8s/               Kustomize base + overlays
   deploy/platform/          pinned Helm values for operators and observability
   perf/                     k6 scripts and workload definitions
   chaos/                    failure-lab experiments
   docs/                     this documentation
   ```

2. **One deployable per logical service** by default. Every service has its own image, Deployment, DB role, and schema.
3. **M2 co-deploy option:** users-svc, orgs-svc, and authz-svc *may* run as one process (`identity-svc`) that mounts three routers, while keeping separate packages, schemas, DB roles, and outboxes. Splitting later is a deployment change, not a refactor. Make this choice at M2 start based on team capacity.
4. **Per-service CI:** path filters build and test only the affected services and libs. A full suite runs on the main branch.

## Consequences

- Shared tooling and atomic contract changes across services.
- The import linter must enforce that services do not import each other's packages; they import only `libs/`.
