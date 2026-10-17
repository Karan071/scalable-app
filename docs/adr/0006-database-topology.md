# ADR-0006: Database topology — shared cluster, schema and role per service

- **Status:** Proposed
- **Date:** 2026-10-17

## Context

PRD §4.3 allows multiple service-owned schemas in one PostgreSQL deployment at first: "logical ownership first, physical separation when needed". Services must not write each other's tables.

## Decision

- **Cluster:** one PostgreSQL 17 cluster managed by CloudNativePG (1 primary, 2 replicas), with a PgBouncer `Pooler` in transaction mode.
- **Databases:**
  - `scaleforge` with one schema per service.
  - `keycloak` as a separate database with its own role.
- **Roles:**
  - Each service gets a login role `svc_<name>` that owns its schema.
  - Default privileges grant nothing to other roles. `search_path` is pinned to the service's schema.
  - Exception: `authz_reader` (ADR-0004).
- **Migrations:**
  - Alembic per service, own version table in its schema.
  - Run as a Kubernetes Job (or Compose one-shot) before the rollout, never at app startup.
  - Expand/contract for zero-downtime rolling deploys.
- **References:** no cross-schema FKs or joins. CI checks the migration SQL for references to other schemas.

## Separation criteria (when to give a service its own cluster)

Any of:

1. The service's write load causes measurable p95 regression for T0 services: replication lag > 1 s sustained, or lock waits affecting others.
2. Different backup/retention/RPO needs. Audit is the likely first candidate.
3. Different scaling profile, such as a read-replica fleet just for search.
4. A security boundary requirement, such as separate encryption keys or access.

## Alternatives

| Option | Why not |
| --- | --- |
| Database per service from day one | Many clusters to run, back up, and monitor before any measured need |
| One shared schema | No enforceable ownership |

## Consequences

- One failure domain for all services' data. Mitigated by HA replicas and PITR.
- Cross-service reporting must use APIs or events, not SQL joins.
- Connection budget is shared and must be planned (LLD-09 §2).

## Revisit triggers

The separation criteria above.
