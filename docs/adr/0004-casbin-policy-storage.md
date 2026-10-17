# ADR-0004: Casbin policy storage and propagation

- **Status:** Proposed
- **Date:** 2026-10-17

## Context

PRD §3.3 asks for decisions on policy storage, reload, synchronization, revocation, and cache invalidation. A PostgreSQL-backed adapter is the candidate.

## Decision

1. **Source of truth:**
   - Relational tables in the `authz` schema (`roles`, `permissions`, `role_permissions`, `role_assignments`, `domain_status`) hold the source of truth, with audit columns and constraints.
   - The Casbin `casbin_rule` table is a **compiled projection**, written in the same transaction by authz-svc. Only authz-svc writes either.
2. **Adapter:**
   - Casbin's SQLAlchemy adapter, read-only for enforcers.
   - Enforcers connect with a dedicated read-only DB role (`authz_reader`) that may `SELECT` only `casbin_rule`, `domain_status`, and `policy_version`.
3. **Versioning:** a single-row `policy_version` counter is incremented on every policy change.
4. **Propagation:**
   - After commit, authz-svc publishes `{version}` on the redis-cache channel `authz:policy`.
   - Enforcers subscribe (watcher) and reload when the version is newer.
   - Enforcers also **poll `policy_version` every 10 s** as a fallback.
5. **Staleness bound:** if an enforcer cannot confirm it is current for > 60 s, it denies privileged and mutating actions. Past > 300 s it fails readiness.
6. **Revocation target:** ≤ 5 s p99 from commit to enforcement (normal), ≤ 15 s with redis-cache down. Measured in M2.

## Alternatives

| Option | Why not |
| --- | --- |
| File-based policies (CSV in ConfigMap) | Role assignments change at runtime through admin APIs |
| `casbin_rule` as the only storage | Generic `v0..v5` columns lose integrity constraints, audit metadata, and readable queries |
| Casbin watcher over RabbitMQ | Works, but couples authz propagation to broker health. Redis pub/sub + DB polling gives two independent paths. |
| Per-request DB policy lookup | Adds latency and DB load to every request |

## Consequences

- Full policy reload is O(policy size). An estimated 50k rules loads in under a second (to be measured). Filtered loading is a later optimization.
- Two propagation paths to test (watcher, polling).
- Enforcers need DB read access to the `authz` schema. This is a documented exception to "read through APIs only", limited to the policy projection tables.

## Revisit triggers

- Policy size makes full reloads exceed 1 s.
- Revocation targets tighten below 1 s.
