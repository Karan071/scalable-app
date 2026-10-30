# Browse baseline (E4.1, LLD-14 §6) — 2026-10-29

## 1. Question / hypothesis

What does the Browse scenario (LLD-14 §6.1, 60% of the real mix) look like under
a low, steady rate, before any optimization work? This is the first real k6
run against the stack — a baseline, not a capacity test.

## 2. Environment

- Local Compose (`make dev`), isolated test DB/vhost (`make test-env`), **not**
  production-representative (ADR-0014) — single Postgres container, one
  replica of each service, host-process services (no containers), one
  developer machine also running other work during the test.
- Commit: `cd4905f255f4cdac98b8caddaa7baaaefd5ad9e4` (branch `develop`).
- Real Keycloak (not the fake IdP), real catalog-svc, search-svc, users-svc,
  bff — one replica each, started as plain `uvicorn` processes
  (`scripts/perf/seed_browse_baseline.py` + manually-started services; see
  that script's own docstring for the exact env each needs).
- 50 real Keycloak users, 40 seeded products (1–3 SKUs each), 4 categories —
  `uv run python scripts/perf/seed_browse_baseline.py --users 50 --products 40`.
- k6 v2.3.0.

## 3. Workload

`k6/browse.js` — the Browse scenario only (categories → search → 2–4 product
details, 3–8s think time), `constant-arrival-rate`, smoke profile (LLD-14
§6.3): 1 iteration/s, 5 minutes, up to 40 preallocated / 60 max VUs. Every VU
logs in once through the real Keycloak login form (not per iteration — LLD-14
§6.1's own "login load is tested separately"), using a distinct synthetic
source IP per VU so the BFF's own auth rate limit (LLD-09 §7.2, 20/min/IP)
doesn't throttle the test itself rather than the system under test.

Search, Cart, Checkout, and Admin (the other four scenarios in LLD-14 §6.1's
table) are **not** in this run — see §9.

## 4. Baseline results

| Metric | Value |
| --- | --- |
| Iterations completed | 297 (of ~300 scheduled) |
| Total requests | 1,672 |
| Error rate (`http_req_failed`) | 7.29% (122/1,672) |
| `categories` p95 | **2.01s** (budget: 250ms) |
| `search` p95 | 23.8ms (budget: 400ms) ✓ |
| `product_detail` p95 | **2.01s** (budget: 250ms) |
| `categories` success rate | 76% (228/300) |
| `product_detail` success rate | 94% (862/912) |

The latency distribution is bimodal, not uniformly slow: median latency for
every route is 11–16ms (fast), but a real minority of `categories` and
`product_detail` calls land at almost exactly **2.01s** — not spread across
1–3s, clustered right at one number.

## 5. What that number is

2.0s is `sf_db.build_engine`'s own default `pool_timeout`. The read pattern
matches a saturated connection pool exactly: most requests get a pooled
connection immediately (fast), and once concurrent demand exceeds the pool's
size, the rest queue up to `pool_timeout` and then either get a connection
right at the deadline or fail with the `503 db-busy` LLD-09 §2 designed for
this exact situation. This wasn't confirmed against catalog-svc's live pool
metrics in this run (no Grafana/Prometheus query was pulled mid-test — a gap
in this run, not a deferred feature), but the timing signature is specific
enough that it's the leading explanation, not a guess from nothing.

This is itself the right kind of baseline finding: the fail-fast mechanism
behaved as designed (bounded wait, a clean `503`, no hung requests, no
cascading failure into other routes — `search`, on a separate, apparently
less-contended path, stayed fast throughout) — it's just that **one
replica's connection pool, at its current size, is undersized for ~25–30
concurrent browsing sessions.** That's a real, load-bearing number this
session didn't have before this run.

## 6. Change applied

None. This is the baseline run; no optimization was attempted in this pass.

## 7. Decision

**Keep** the result as this epic's first baseline, and treat catalog-svc's
connection pool sizing as the first concrete candidate for E4.2 (EXP-3,
"Connection pooling," LLD-09 §9) once that epic starts — this run is the
"before" data point that experiment needs. Not escalated further in this
session: confirming the exact bottleneck (versus, say, FastAPI's own
concurrency limits or the bulkhead in front of the DB pool) needs a
follow-up run with Postgres/pool metrics actually pulled during the test,
which this pass didn't do.

## 8. Limitations of this test

- Single Postgres container and single service replica each — not
  production topology.
- No DB/pool metrics captured during the run (see §5) — the 2.01s
  explanation is inferred from timing, not confirmed against
  `db_pool_timeouts_total` or Postgres's own `pg_stat_activity`.
- Ran on a developer machine also running other local work during the
  test window — some noise is possible, though the clean bimodal split
  (fast vs. exactly-2.01s, nothing in between) doesn't look like generic
  machine noise.
- Only the Browse scenario; see §9 for what a real mix run still needs.
- 50 users is enough to avoid the IP/session-collision issues found while
  building this script (see `scripts/perf/seed_browse_baseline.py` and
  `k6/browse.js`'s own comments) but is still far fewer than a real
  storefront's concurrent user count — not a statement about real
  production capacity.

## 9. Still open (E4.1/E4.2)

- Search, Cart, Checkout, Admin scenarios and the weighted `mix` script
  (LLD-14 §6.1's full table) — only Browse exists so far.
- Step/knee, soak, and spike profiles (LLD-14 §6.3) — only smoke has run.
- EXP-1 through EXP-9 (LLD-09 §9) — none attempted; this report's §5 finding
  is the first candidate for EXP-3 specifically.
- Rate-limit calibration against a real mix (the gap E4.4's own delivery-plan
  note already named) — still blocked on a fuller workload than Browse alone.
