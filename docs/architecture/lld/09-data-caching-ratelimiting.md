# LLD-09: Data Platform, Query Design, Caching, Rate Limiting, and Performance Experiments

| Field | Value |
| --- | --- |
| Status | Draft v0.1 |
| Milestone | M3 (DB rules) → M4 (cache, rate limits, experiments) |
| Components | PostgreSQL (CNPG + PgBouncer), redis-cache, `sf-db`, `sf-common` |
| ADRs | 0006, 0010, 0015 |

## 1. PostgreSQL topology, roles, and migrations

| Item | Design |
| --- | --- |
| Cluster | CloudNativePG, PostgreSQL 17, 1 primary + 2 replicas (staging); 1 instance (local kind optional) |
| Services | `-rw` (primary), `-ro` (replicas). Poolers: `pooler-rw`, `pooler-ro` (PgBouncer, transaction mode). |
| Databases | `scaleforge` (service schemas), `keycloak` |
| Roles | `svc_<name>` (LOGIN, owns its schema); `authz_reader`; `audit_retention`; `migrator_<name>` (DDL in own schema only). No service uses a superuser. |
| Role settings | `ALTER ROLE svc_x SET statement_timeout = '2s'`, `lock_timeout = '1s'`, `idle_in_transaction_session_timeout = '10s'`, `search_path = <schema>` |
| Extensions | `pg_stat_statements`, `pg_trgm`, `ltree`, `citext`, `btree_gin` (pre-created by the platform, not by services) |
| TLS | Required for client connections (CNPG-managed certificates) |
| Observability | CNPG exporter metrics + `pg_stat_statements`. `auto_explain` (log_min_duration 500 ms) in staging only. |

**Migrations:**

- Alembic per service; the version table lives in the service schema.
- They run as a pre-deploy Job (Compose: a one-shot service) with `lock_timeout = 5s`.
- **Expand/contract** for rolling deploys:
  1. Add nullable columns or new tables.
  2. Deploy code that writes both.
  3. Backfill in batches.
  4. Switch reads.
  5. Drop the old columns in a later release.
- Use `CREATE INDEX CONCURRENTLY` (Alembic: outside a transaction).
- **CI lint** (Squawk) blocks:
  - table rewrites;
  - non-concurrent indexes on existing tables;
  - `NOT NULL` without a default on large tables;
  - references to other schemas.

## 2. Connection pooling

| Layer | Setting (initial) |
| --- | --- |
| App (SQLAlchemy async + asyncpg) | `pool_size = 10`, `max_overflow = 5`, `pool_timeout = 2s` (fail fast → 503 `db-busy`), `pool_pre_ping = true`, `pool_recycle = 1800s` |
| asyncpg + PgBouncer transaction mode | Requires PgBouncer ≥ 1.21 with `max_prepared_statements = 200` (protocol-level prepared statements). Fallback: asyncpg `statement_cache_size = 0`. Verified by an integration test **through the pooler**. |
| PgBouncer | Pools are per (db, user), so per service role: `max_client_conn = 2000`, `query_wait_timeout = 5s`, `reserve_pool_size = 5`. `default_pool_size` per role is set by the budget below. |
| Server budget | See the table below |

**Server connection budget** (`max_connections = 400`):

| Consumer | Pool per role | Roles | Subtotal |
| --- | --- | --- | --- |
| T0 services (catalog, cart, inventory, orders, payments) | 30 | 5 | 150 |
| T1 services (users, orgs, authz, search) | 20 | 4 | 80 |
| T2 services (notifications, audit) | 10 | 2 | 20 |
| Keycloak (`keycloak` DB, own pool) | 30 | 1 | 30 |
| Direct connections: outbox relays (LISTEN), advisory-lock leaders, migrations | — | — | 50 |
| Reserved: superuser and CNPG internals | — | — | 20 |
| Headroom | — | — | 50 |
| **Total** | | | **400** |
| Bulkheads | Separate engines (pools) for request handling and background workers (relay, sweepers, consumers) in each service, so a backlog cannot starve requests. **Implemented (M2):** every service builds one request engine plus one engine per background worker: the outbox relay (`RELAY_POOL_SIZE`, 2) and each inbox consumer (`sf_db.worker_pool(DEFAULT_HANDLER_CONCURRENCY)`, 10, no overflow, 30 s checkout wait instead of failing fast). Verified by `tests/system/test_bulkheads.py`: with Keycloak answering in 5 s and 40 commands queued, requests stay fast. Before the change, the same setup exhausted the shared pool and starved both requests and the relay. |

**Sizing:** by Little's law, `busy_connections ≈ throughput × mean DB time per request`. For example, 300 req/s × 15 ms ≈ 4.5 busy connections. Pools are sized for p99 bursts, then **verified** by EXP-3 (pool wait time histogram, PgBouncer `cl_waiting`).

**Metrics:**

- `db_pool_checked_out{service, pool}`;
- `db_pool_wait_seconds{service, pool}` (histogram, from SQLAlchemy pool events);
- `db_pool_timeouts_total` (implemented: counted by the shared `503 db-busy` handler, which all four DB-backed services install via `sf_db.POOL_TIMEOUT_ERRORS`; `checked_out` and `wait_seconds` are not yet);
- PgBouncer exporter: `cl_waiting`, `sv_active`, `avg_wait_time`.

## 3. Query design rules

| Rule | How it is enforced |
| --- | --- |
| No network calls inside a DB transaction | Code review. `sf-db` `transaction()` context warns when an `sf-common` HTTP client call runs inside it (test mode raises). |
| Tenant filter on every org-scoped query; `org_id` leads composite indexes | `TenantScope` repository API (LLD-02 §5); index review |
| No N+1 | SQLAlchemy relationships default to `lazy="raise"`. Explicit `selectinload`/`joinedload` per query. Cross-service lookups use batch endpoints (`:batch?ids=`). Query-count assertions in integration tests (`assert_max_queries(n)`). |
| Stable pagination | Keyset on `(sort_key, id)` with a signed cursor. `OFFSET` only for capped relevance search. |
| Explicit projections | No `SELECT *` in hot paths. List endpoints select the listed columns. |
| Partial indexes for hot status filters | e.g. `WHERE status = 'active'`, `WHERE published_at IS NULL` |
| Bounded statements | Role `statement_timeout`. Heavier jobs set a higher local timeout explicitly. |
| Plan evidence | Top-N queries by total time (`pg_stat_statements`) reviewed each milestone. `EXPLAIN (ANALYZE, BUFFERS)` attached to experiment reports. |

**Locking guidance:**

| Technique | Use for | Examples |
| --- | --- | --- |
| Optimistic (version column + `If-Match`) | Low-contention human edits | Product, org settings, stock adjust, staff order transitions |
| Pessimistic (`SELECT … FOR UPDATE`, ordered) | High-contention invariants | Stock reservation, idempotency claim, invitation accept, last-admin guard |
| `FOR UPDATE SKIP LOCKED` | Work queues across replicas | Outbox relay, expiry sweeper, saga recovery worker |
| Advisory lock (session, direct connection) | Singleton jobs | Reconciler (LLD-06 §9), audit chain writer |
| Deadlock/serialization retry | Any multi-row write | `sf-db` `@retry_on(40P01, 40001, attempts=3, jitter)` |

## 4. Read replicas (EXP-7)

- **Eligible reads**, declared per route with `replica_ok(max_lag=2s)`:
  - storefront product detail on cache miss;
  - category tree;
  - search queries;
  - audit queries;
  - admin reports.
- **Never on a replica:**
  - checkout, inventory, idempotency, payments;
  - authz policy load (staleness would weaken revocation);
  - any read that follows a write by the same request.
- **Lag guard:** `sf-db` samples `now() - pg_last_xact_replay_timestamp()` on the replica every 5 s. Above `max_lag`, eligible reads route to the primary. Metric: `db_replica_lag_seconds`.
- **Read-your-writes:** admin UIs read from the primary, because admin routes are not `replica_ok`. Storefront staleness ≤ 2 s is acceptable and documented.

## 5. Partitioning (EXP-8)

| Candidate | Key | Why it might help | Adopt if |
| --- | --- | --- | --- |
| `audit.audit_events` | `occurred_at` monthly | Retention by dropping partitions; time-bounded queries prune | > ~50M rows, or retention deletes cause measurable bloat/vacuum lag |
| `*.outbox` | `occurred_at` daily | Cheap 7-day retention | Delete-based cleanup shows vacuum pressure |
| `inventory.stock_movements` | `created_at` monthly | Ledger growth | Reconciliation queries degrade |
| `orders.orders` | `created_at` | Usually **not** helpful: most queries are by customer or org, not time | Only with measured evidence |

The rule (PRD §5.1): partition only when workload and maintenance measurements justify it. EXP-8 compares delete-based retention against partition drops, and query plans with and without pruning, at 10M/50M rows.

## 6. Caching (redis-cache)

### 6.1 What is cached

| Data | Owner (does cache-aside) | Key | TTL | Invalidation | Stale tolerance |
| --- | --- | --- | --- | --- | --- |
| Product detail (storefront) | catalog-svc | `cat:v1:prod:{org}:{slug}` | 300 s ± 10% | Delete after commit + consumer of own `product.updated` (double delete). Old and new slug on rename. | ≤ invalidation latency (~1 s); TTL backstop |
| Category tree | catalog-svc | `cat:v1:cats:{org}` | 600 s ± 10% | Delete on category change | Same |
| Store profile | orgs-svc | `org:v1:profile:{org}` | 600 s ± 10% | Delete on update, suspend, or reactivate | Suspension visible ≤ 1 s typical (the domain_status check is the real control) |
| Availability | inventory-svc | `inv:v1:avail:{org}:{sku}` | 5 s | None (short TTL) | 5 s; checkout re-validates |
| Negative product lookups | catalog-svc | `cat:v1:prod404:{org}:{slug}` | 30 s | Delete on create | 30 s |

**Never cached:** carts, orders, payments, stock for checkout, idempotency records, authz *decisions*, sessions (redis-session), and anything per customer.

### 6.2 Consistency details

- **Versioned values:** a cached value includes `version`. Writes go through a Lua "set if newer or absent" script. This prevents the classic race where a slow reader re-populates a stale value after the writer's delete.
- **Key versioning** (`v1`) allows a cache-format change to roll out without flushing.

### 6.3 Stampede protection

1. **TTL jitter** (± 10%).
2. **Soft TTL / stale-while-revalidate:** the value stores `soft_exp = now + 0.8 × TTL`. After `soft_exp`, the first request that wins a refresh lock refreshes in the background while others are served the cached value.
3. **Cross-pod refresh lock:** `SET lock:{key} <token> NX PX 2000`. Losers on a **hard** miss wait up to 150 ms polling for the value, then fall through to the DB under the concurrency limiter.
4. **Per-pod single-flight:** one in-flight load per key per process.

### 6.4 Redis-cache outage

- A circuit breaker on the cache client (open after 5 failures in 10 s, half-open probe after 30 s) bypasses the cache.
- **DB protection:** a per-pod semaphore (20) for cache-miss loads. When saturated, return 503 `Retry-After: 1` rather than overload PostgreSQL.
- Metric `cache_bypass_total` and alert.

### 6.5 Metrics

- `cache_requests_total{cache, result}` (hit, miss, stale_served, bypass);
- `cache_load_duration_seconds{cache}`;
- `cache_stampede_waits_total{cache}`.

Labels are bounded by cache name, never by key.

## 7. Rate limiting

### 7.1 Placement

| Layer | Purpose | Implementation |
| --- | --- | --- |
| Edge (Envoy Gateway) | Coarse per-IP flood control on all routes | Global rate limit backed by redis-cache: 1,200 req/min per client IP, burst 200 |
| BFF | Per-class business limits with user/org/IP keys | GCRA in a Redis Lua script (one key per bucket, atomic, O(1)) |
| Keycloak | Password and OTP brute force | Keycloak brute-force detection (lockout with backoff) |
| Services | Expensive internal operations | Reuse the BFF classes for admin exports and reindex triggers |

### 7.2 Classes (initial values, calibrated in M4)

| Class | Routes | Limit | Key dimensions |
| --- | --- | --- | --- |
| `auth` | `/auth/login`, `/auth/callback`, `/auth/step-up` | 20/min | IP |
| `public` | Storefront reads | 300/min, burst 60 | IP (IPv6 /64) |
| `search` | Search | 60/min, burst 20 | user if authenticated, else IP |
| `checkout` | Checkout, order cancel | 10/min | user; plus 30/min per IP |
| `mutation` | Other authenticated writes | 120/min | user |
| `enumeration` | Handle availability, invitation accept | 10/min and 50/hour | user; plus IP |
| `admin_expensive` | Exports, reindex, bulk updates, audit queries | 10/min per user; 100/hour per org | user, org |
| `sse` | `/api/events` | 3 concurrent streams | user (concurrency counter, not a rate) |

- **Client IP:** taken from `X-Forwarded-For` **only** at the configured trusted hop count (1: the gateway). Never from arbitrary headers.
- **Responses:** `429 application/problem+json` with type `rate-limited`, `Retry-After`, and the IETF draft headers `RateLimit-Policy` / `RateLimit`. The response is identical across replicas, because the state is shared in Redis.
- **Redis-cache outage:** fall back to an in-process GCRA at `limit / replica_count` (approximate). For `auth` and `enumeration`, use `limit / (2 × replica_count)`, so the fallback errs on the strict side. Metric `ratelimit_fallback_active`.
- **Metrics:** `ratelimit_decisions_total{class, result}`. Rejections are logged sampled, with no user IDs in labels.

## 8. Bloom filter (EXP-6, optional)

- **Target:** handle availability checks (`/v1/me/handle-availability`). The DB unique constraint remains the authority for registration (PRD §5.3).
- **Design:**
  - Redis 8 Bloom filter `bf:handles`: capacity 1M, error rate 0.1% (≈ 1.8 MB).
  - **Init:** a Job streams `handle_normalized` → `BF.MADD`.
  - **Update:** on handle set → `BF.ADD`.
  - **Deletion:** not supported by Bloom filters, so rebuild the filter nightly into `bf:handles:new` and `RENAME`.
  - **Comparison:** a Cuckoo filter (supports delete) is tested in the same experiment.
- **Lookup:**
  - `BF.EXISTS = 0` → definitely free → respond `available` without a DB read.
  - `= 1` → DB check.
  - Redis error → DB check.
- **Privacy:** the filter is never exposed. The response time floor (LLD-01 §11.3) hides the fast path.
- **Measure:** DB reads avoided, p95 latency, observed false-positive rate, memory, rebuild time.
- **Expected:** an indexed unique lookup is already cheap, so the benefit may be negligible. Reporting "rejected, with data" is a valid outcome.

## 9. Performance experiments (PRD §5.2)

Every experiment follows the LLD-14 §6.4 report template: hypothesis, environment, workload, baseline, change, results (p50/p95/p99, throughput, errors, resources), decision, and rollback.

| ID | Experiment | Hypothesis | Method | Success evidence |
| --- | --- | --- | --- | --- |
| EXP-1 | N+1 elimination | Admin product list (page 50) issues 1 + 3N queries with lazy loading | Query count + latency before/after `selectinload`; `lazy="raise"` | Queries ≤ 3 at any N; p95 reduction reported |
| EXP-2 | Indexing | Org-scoped listing and search sort lack suitable composite indexes | `EXPLAIN (ANALYZE, BUFFERS)` before/after; write cost (insert latency), index size | Plan changes from seq scan to index scan; documented write overhead |
| EXP-3 | Connection pooling | App pool + PgBouncer keeps DB connections bounded while throughput scales | Step load; vary `pool_size` (5/10/20) with PgBouncer on and off; measure pool wait, `cl_waiting`, server connections, p95 | Stable utilization with no connection storms; chosen sizes justified |
| EXP-4 | Redis cache | Product detail cache reduces DB reads by > 80% at a Zipf-distributed workload | Cold vs warm, hit rate, DB QPS, p95; stampede test (expire a hot key under load) | Measured DB read reduction; no stampede spike |
| EXP-5 | Debounced search | 300 ms debounce + request cancellation cuts requests per typed query by > 60% | Playwright typing script measuring request counts; user-perceived latency | Fewer requests with acceptable responsiveness |
| EXP-6 | Bloom filter | Handle availability DB reads reduced | §8 | Useful reduction, or documented rejection |
| EXP-7 | Read replicas | Offloading storefront reads reduces primary CPU, with lag ≤ 2 s | Replica lag under write load; read latency on primary vs replica | Documented freshness/scale trade-off |
| EXP-8 | Partitioning | Audit/outbox retention via partition drop beats DELETE + VACUUM | 10M/50M rows; retention job time, bloat, query plans | Evidence for or against adoption |
| EXP-9 | Hot-SKU contention | LLD-07 §10 | LLD-07 §10 | LLD-07 §10 |

**Rule:** E4.1 baselines run before any experiment. A feature such as the cache or the Bloom filter is kept only with positive evidence (PRD §1 "evidence-based optimization").

## 10. Requirements covered

PRD §5.1 (all bullets), §5.2, §5.3, §5.4 (all bullets); SEC-06, SEC-11; Appendix A (rate limiting, caching, cache invalidation, connection pooling, N+1, indexing, read replicas, replication, partitioning, Bloom filters, optimistic/pessimistic locking, distributed locks).

## 11. Tests

| Test | Type |
| --- | --- |
| Pooler compatibility: prepared statements through PgBouncer transaction mode | Integration |
| Pool exhaustion → fast 503 (not a hang); recovery | Resilience |
| `TenantScope` lint: unscoped repository use fails CI | Static |
| Query-count assertions on list endpoints | Integration |
| Cache: stale-repopulation race (delay the reader, write, then the reader sets) → the versioned set rejects stale | Concurrency |
| Cache stampede: 500 concurrent requests on an expired hot key → ≤ 2 DB loads | Concurrency |
| Redis-cache down → bypass, DB guard, 503 when saturated, fallback rate limits | Resilience (FS-1 variant) |
| Rate limits: 429 with headers; consistent across 3 BFF replicas; key dimensions correct; XFF spoofing ignored | API |
| Migration lint catches a non-concurrent index | CI |
| Replica lag guard routes to the primary when lag > threshold (pause replay on the replica) | Integration |
