# ADR-0010: Redis topology — separate session and cache instances

- **Status:** Accepted (2026-10-17)
- **Date:** 2026-10-17

## Context

Redis holds sessions, cache, and rate-limit data (PRD §13.3), and is named a critical-dependency risk (PRD §13.2). Sessions need durability and must never be evicted. Cache data must be evictable. Rate limits must work across replicas.

## Decision

| Instance | Contents | Eviction | Persistence | HA | Outage behavior |
| --- | --- | --- | --- | --- | --- |
| **redis-session** | Sessions, user→session index, OIDC pre-auth state, refresh locks | `noeviction` | AOF `everysec` + RDB | 1 primary + 2 replicas + 3 Sentinels | Protected operations return 503 (never treat the session as valid or invalid by guess). Public reads continue. |
| **redis-cache** | Cache-aside entries, rate-limit buckets, `authz:policy` channel, SSE fan-out, Bloom experiment | `allkeys-lru` | None | Single primary (+ replica in staging) | Cache bypass with DB concurrency guard; local per-pod rate limiter at `limit / replicas`; policy polling fallback; SSE degrades to long-poll |

- **Image:** the official Redis image (Redis 8.x; Bloom filters built in). The Bitnami chart is avoided (HLD §15).
- **Access:**
  - ACL users per client class (`bff`, `svc-cache`).
  - TLS in staging.
  - NetworkPolicy allows only the listed clients.

## Alternatives

| Option | Why not |
| --- | --- |
| One Redis for everything | Cache memory pressure could evict sessions (unless `noeviction`, which would then fail cache writes). Shared failure domain. |
| Redis Cluster (sharded) | Not needed at expected sizes. Complicates multi-key Lua scripts. |
| Valkey | License-friendly fork and a viable alternative. The PRD names Redis; revisit if licensing matters. |
| Sessions in PostgreSQL | Durable, but adds DB load on every request. The PRD selects Redis. |

## Consequences

- Two Redis deployments to run.
- Sentinel-aware clients are required in the BFF.
- Failover takes ~10–30 s. During failover, protected requests get 503 and the frontend shows a retry notice.
