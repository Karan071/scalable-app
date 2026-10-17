# LLD-13: Reliability, Failure Lab, Backup/Restore, and Disaster Recovery

| Field | Value |
| --- | --- |
| Status | Draft v0.1 |
| Milestone | M4 (resilience patterns, Compose failure tests) → M6 (in-cluster failure lab, DR drills) |
| ADRs | 0003, 0006, 0010 |

## 1. Scope

This LLD covers:

- resilience parameters for every outbound interaction;
- the failure-scenario catalog with pass criteria (PRD §9.4);
- failure-lab tooling and process;
- backup, restore, and DR targets (PRD §9.5);
- clock-skew handling;
- the multi-region paper design;
- the serverless cold-start experiment.

## 2. Resilience parameters (`sf-common`)

### 2.1 Timeouts

The budgets are in HLD §9.1. Every client sets connect (≤ 500 ms) and total timeouts. The BFF sets `X-Request-Deadline` (an epoch-ms absolute deadline). Each hop computes `remaining = deadline − now − 50 ms safety`. If `remaining` is less than the hop's minimum, it fails fast with 503 `deadline-exceeded` instead of making the call.

### 2.2 Retries

| Setting | Value |
| --- | --- |
| Eligible | GET/HEAD; PUT/DELETE (idempotent by design); POST **only** with an `Idempotency-Key` or to an endpoint documented as idempotent by business key |
| Retry on | Connect errors, 502/503/504, 429 (honor `Retry-After` if ≤ the remaining budget) |
| Never retry on | Other 4xx; timeouts on non-idempotent calls; anything after the deadline |
| Attempts | 3 total (1 + 2 retries) within the deadline |
| Backoff | Full jitter: `sleep = random(0, min(cap, base × 2^attempt))`, base 100 ms, cap 1 s |
| Retry budget | Per client: retries ≤ 10% of requests over 10 s (token bucket). Prevents retry storms during an outage. |

### 2.3 Circuit breakers

These apply per dependency (host + operation class):

| Setting | Value |
| --- | --- |
| Window | Rolling 10 s, minimum 10 calls |
| Open when | Failure rate ≥ 50%, or slow-call rate ≥ 80% (slow = > 80% of the timeout) |
| Open duration | 15 s, then half-open |
| Half-open | 3 probe calls; all succeed → closed, any fails → open |
| When open | Fail fast with a typed error → 503 `dependency-unavailable` (or the documented degradation, e.g. availability unknown) |

Metric: `circuit_breaker_state{service, dependency}` (0 closed, 1 half-open, 2 open).

### 2.4 Bulkheads and load shedding

- **Per-dependency concurrency semaphores** (per pod):
  - orders → payments: 50;
  - orders → inventory: 100;
  - BFF → each service: 200.

  When full, the call fails fast (503) instead of queueing.
- **Pod-level in-flight cap:** BFF 400, services 200. Beyond the cap the response is 503 + `Retry-After: 1`. Health endpoints are exempt.
- **Separate DB pools** for requests vs background workers (LLD-09 §2).

## 3. Failure-scenario catalog

Injection tools: **Compose** uses Toxiproxy and `docker stop/pause`. **Kubernetes** uses Chaos Mesh (`PodChaos`, `NetworkChaos`, `StressChaos`, `TimeChaos`) or `kubectl`. Every scenario runs under a background k6 load (a baseline mix at 30% of the measured knee) unless stated otherwise.

| ID | Scenario (PRD §9.4) | Injection | Expected behavior | Signals | Pass criteria |
| --- | --- | --- | --- | --- | --- |
| FS-1 | Redis outage | (a) stop redis-session primary (Sentinel failover); (b) stop all redis-session; (c) stop redis-cache | (a) 503 on protected routes during failover ≤ 30 s, then recovery; revocations not lost (`WAIT`). (b) Protected routes 503, public reads OK, no request treated as authenticated. (c) Cache bypass, DB guard, fallback rate limits, policy polling, SSE → long-poll. | `bff_session_validation_total{store_unavailable}`, breaker state, `cache_bypass_total`, alerts | No 2xx on protected routes without a validated session; recovery automatic; DB CPU below saturation in (c) |
| FS-2 | PostgreSQL outage | (a) kill the primary pod (CNPG failover); (b) stop all instances | (a) Errors during failover ≤ 60 s, then recovery; no data loss for committed transactions. (b) Fast 503s (pool timeout 2 s), readiness fails → gateway 503; no hung threads. | DB errors, readiness, `db_pool_timeouts_total` | (a) RTO ≤ 60 s measured; (b) p99 error response < 3 s; full recovery without manual restarts |
| FS-3 | Slow dependency | Toxiproxy latency 3 s on payments → provider; then inventory | Timeouts fire; the breaker opens; checkout returns 202 pending (payment) or 503 fast (inventory); no thread or pool exhaustion elsewhere | Breaker state, client latency histograms, in-flight | Other endpoints keep p95 within 20% of baseline; no cascading 5xx on unrelated routes |
| FS-4 | Broker outage | Stop RabbitMQ (all nodes) for 5 min; also a 1-of-3 node loss | Sync paths work; outbox backlog grows; after recovery the backlog drains; no lost events. A single-node loss is transparent. | `outbox_pending`, `outbox_oldest_unpublished_age_seconds`, alerts | Every committed event delivered after recovery (count reconciliation); sagas complete |
| FS-5 | Duplicate event | Republish N random messages (replay tool) + consumer crash before ack | Duplicates are no-ops | `consumer_messages_total{duplicate}` | No duplicate side effects: DB invariants, one email per dedupe key |
| FS-6 | Pod termination | Delete pods of each T0 service under load; drain a node | Terminating pod leaves endpoints before shutdown; replacement ready; in-flight requests complete | Readiness, 5xx rate, restarts | 5xx ≤ in-flight count at kill time (≈ 0 with idempotent retries at the BFF) |
| FS-7 | Concurrent checkout | Oversell test (LLD-07 §13) at scale + duplicate submissions | Invariants hold | `inventory_conflicts_total`, ledger reconciliation | Zero oversell; one order per idempotency key/cart version |
| FS-8 | Payment timeout / duplicate callback | Mock scenarios `timeout`, `duplicate_webhook`, `out_of_order`, `delayed_webhook` | Saga reaches the correct terminal state; no double capture | Saga metrics, reconciliation | Payment ledger: ≤ 1 capture per order; order ↔ payment consistent |
| FS-9 | Stale cache | Delay the cache delete (fault hook) and race readers; expire a hot key under load | Versioned set rejects stale; TTL bounds staleness; stampede protected | Cache metrics | Staleness ≤ documented bound; ≤ 2 DB loads per hot-key expiry |
| FS-10 | Role revocation | Revoke a role under traffic from that user; with redis-cache down as a variant | Access removed within target | `authz_revocation_propagation_seconds` | p99 ≤ 5 s (≤ 15 s variant) |
| FS-11 | Network partition | Chaos Mesh partition: (a) orders ↔ inventory; (b) one RabbitMQ node isolated; (c) one PG replica isolated | (a) Checkout fails fast or pends, no inconsistency. (b) The minority node's queues are unavailable; clients reconnect to the majority. (c) Lag guard routes reads to the primary. | Breakers, lag, queue metrics | No invariant violations; documented CP/AP behavior (HLD §8.4) observed |
| FS-12 | Backup restoration | §5.4 drill | Data restored; integrity verified | Drill report | Measured RPO ≤ target, RTO ≤ target, integrity checks pass |
| FS-13 | Keycloak outage (addition) | Stop all Keycloak pods for 10 min | Existing sessions work until access-token expiry (≤ 5 min), then protected routes return 503 (refresh fails); new logins fail with a friendly error; recovery automatic | `bff_token_refresh_total{timeout}` | No session is treated as valid without a valid token; no data corruption |
| FS-14 | Telemetry pipeline down (addition) | Stop the Collector gateway | Business traffic unaffected; loss visible | `otelcol_*`, SDK drop counters | No latency or availability regression beyond noise |

## 4. Failure lab and chaos process

- **Experiment template** (`chaos/<id>/README.md`):
  - steady-state hypothesis (metrics + thresholds);
  - blast radius;
  - abort conditions (for example, an error rate > 20% for 2 min, or any invariant check failing);
  - injection steps;
  - rollback;
  - observations.
- **Cadence:**
  - **M4:** FS-1..FS-9 automated in Compose, nightly.
  - **M6:** the full catalog in kind/staging. A game day per milestone afterwards.
- **Runbooks:** `docs/runbooks/` (planned), one per alert (LLD-10 §9) and per scenario. Each covers symptoms, dashboards, diagnosis steps, mitigation, and verification.
- **Incident and experiment reports:** `docs/reports/incidents/` use a template with timeline, impact, detection (how and how fast), root cause, contributing factors, what went well, and actions with owners.

## 5. Backup, restore, and disaster recovery

### 5.1 Targets (single region)

| Data | RPO | RTO | Backup method | Notes |
| --- | --- | --- | --- | --- |
| PostgreSQL (`scaleforge`, `keycloak`) | ≤ 5 min (WAL `archive_timeout = 60s`) | ≤ 60 min full restore; ≤ 60 s HA failover | CNPG Barman Cloud plugin: daily base backup + continuous WAL to object storage, 14-day retention, encrypted | PITR to any point within retention |
| Keycloak config | 0 (realm as code) + DB above | ≤ 30 min | Git + PG backup | Secrets restored from the secret store |
| redis-session | Not backed up for DR | ≤ 15 min (empty start) | AOF for node restarts only | DR = users re-login (accepted, documented) |
| redis-cache | n/a | Minutes (empty start) | none | Rebuilds lazily |
| RabbitMQ | Definitions: 0 (as code). Messages: recovered by outbox replay within 7 days. | ≤ 30 min | Topology CRs/definitions in Git | Broker is not a system of record |
| Object storage (images) | ≤ 24 h | ≤ 2 h | Bucket versioning + nightly mirror to a second bucket/location | |
| Audit | Same as PostgreSQL + monthly NDJSON exports | — | | |

The numbers are targets. The drill measures the actuals (PRD §9.5).

### 5.2 Backup security

- A separate bucket and credentials. The cluster's credentials can write and read but not delete; lifecycle rules handle expiry.
- Encryption at rest.
- Access is audited.
- Backups are verified weekly by automated restore.

### 5.3 Failover (in-region)

- CNPG promotes a replica automatically on primary failure. Services reconnect via the `-rw` service and poolers.
- Measured in FS-2(a).
- Redis Sentinel failover is measured in FS-1(a).
- A RabbitMQ quorum leader change is transparent.

### 5.4 Restore drill (FS-12)

1. **Pick a target time T** and record the expected state at T (snapshot of counts, recent IDs, and checksums for key tables).
2. **Restore** a new CNPG cluster from backup with PITR to T, in an isolated namespace. Time it.
3. **Integrity checks:**
   - row counts and checksums vs the expected state;
   - FK/constraint validation;
   - inventory ledger reconciliation (LLD-07 §9);
   - payment ledger;
   - audit hash-chain verification;
   - outbox and inbox sanity.
4. **Switch the app** to the restored cluster (staging), with checkout disabled by the flag `checkout.enabled=false`.
5. **Post-recovery reconciliation** (§5.5), then re-enable checkout.
6. **Report** the measured RPO (data lost after T, or the gap between the last archived WAL and failure), RTO (detection → service restored), issues, and actions.

### 5.5 Post-recovery reconciliation

After restoring to time T, **downstream state may be newer than the DB**: consumers applied events after T, and the external provider holds payments after T.

| Area | Action |
| --- | --- |
| Payments vs provider | R2/R4/R5 against provider state (the provider is the source of truth for money). Orders missing locally but paid at the provider → refund, and record for manual follow-up. |
| Search index | Full reindex from catalog (LLD-05 §9) |
| Inventory | Ledger checks. Reservations without orders → release. |
| authz policy | Enforcers reload on version change (the version may go backwards after a restore). Enforcers must accept a lower version when `policy_epoch` changes. `policy_epoch` is a UUID regenerated on restore by the restore runbook. |
| Sessions | Invalidate all sessions (flush redis-session) when the identity data restore point is older than recent revocations |
| Outbox | Events in the restored DB that are not yet published are published normally. Consumers dedupe. |

### 5.6 Clock-skew note for DR

Restored systems may start with different clocks. Time-based checks rely on DB time (§6).

## 6. Clock-skew handling (PRD §9.5)

| Concern | Risk | Mitigation | Test |
| --- | --- | --- | --- |
| Session idle/absolute expiry | Replicas disagree on expiry | Session timestamps and comparisons use **Redis server time** (`TIME`, pipelined with the session read in one round trip) | TimeChaos +5 min on one BFF pod → same decisions |
| JWT `exp`/`nbf`/`iat` | Valid tokens rejected or expired ones accepted | NTP on nodes; leeway 30 s (services) / 60 s (ID token at BFF); alert on node clock offset > 1 s (node-exporter `node_timex_offset_seconds`) | Skew a service pod ±90 s → rejections observed and documented |
| Webhook timestamps | Valid callbacks rejected | 300 s tolerance; provider retries | Skew payments pod +6 min → rejected; recovers when skew is removed |
| Reservation expiry, saga deadlines, invitation expiry, idempotency TTLs | Early or late expiry | Evaluated with PostgreSQL `now()` | Skew app pods → no change in behavior |
| Event ordering | Timestamps misorder events | Ordering by aggregate version, never by `time` | Out-of-order tests (LLD-08 §14) |
| Scheduled jobs | Double or skipped runs | Idempotent jobs; advisory-lock singletons; `startingDeadlineSeconds` on CronJobs | Two replicas + skew → one run |
| Logs and traces | Confusing timelines | Collector adds `observed_time`. Span durations use monotonic clocks per process. | Review |

## 7. Multi-region (paper design, deferred)

The precondition is that the §5.4 drill passes with the RPO/RTO targets met.

| Aspect | Proposed approach |
| --- | --- |
| Topology | Active-passive: region B runs a CNPG **replica cluster** (streaming/WAL-shipping from A), scaled-down app deployments, and its own Redis and RabbitMQ |
| Writes | Region A only. Failover promotes region B's replica cluster (manual decision, scripted). |
| Sessions | Regional. After a failover, users re-login (redis-session is not replicated). Alternative: replicate sessions asynchronously, at a security cost (revocations may lag). |
| Messaging | No cross-region broker. After a failover, region B republishes unpublished outbox rows. |
| Identity | Keycloak in B uses the replicated `keycloak` DB |
| DNS / edge | Health-checked DNS failover (low TTL) or a global load balancer |
| RPO / RTO | RPO = replication lag (seconds, measured); RTO ≈ 30 min (promotion + DNS + warm-up) |
| Consistency | Single writer, so no conflict resolution is needed. Reads in B during normal operation are stale-tolerant only. |
| Cost | Roughly 1.5–2× infrastructure for standby compute and storage; to be estimated per provider |
| Testing | Region-failover game day in a cloud environment only |

## 8. Serverless cold-start experiment (optional)

- **Question:** what latency and cost trade-off does scale-to-zero give for a bursty, low-volume workload?
- **Candidate workload:** the image re-encode worker (LLD-05 §5) or the email sender.
- **Setup:** Knative Serving on kind (scale-to-zero, `containerConcurrency = 10`) vs an always-on Deployment. Triggered by HTTP (a RabbitMQ → HTTP bridge, or a KEDA HTTP add-on as an alternative).
- **Measure:**
  - cold-start latency (p50/p95/p99 for first request after idle);
  - warm latency;
  - resource-seconds consumed over a 24 h synthetic pattern;
  - DB connection behavior on scale-from-zero (needs the pooler).
- **Report limits:** execution time caps, connection storms, local state loss, and observability gaps.

## 9. Requirements covered

PRD §9.4 (all scenarios), §9.5 (all bullets), §4.4 (timeouts, retries, backoff); NFR Reliability; Appendix A (circuit breakers, timeouts, retries, backoff, backup, DR, failover, multi-region, chaos engineering, cold starts, serverless limits, network partitions, clock skew).
