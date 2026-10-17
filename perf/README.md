# Performance workspace

Scaffold for the performance work described in [LLD-14](../docs/architecture/lld/14-testing-strategy.md) and the experiments in [LLD-09](../docs/architecture/lld/09-data-caching-ratelimiting.md).

- `k6/scenarios/` — one k6 script per user journey and a mixed workload.
- `workloads/` — workload profiles for smoke, baseline, capacity, soak, and spike runs.
- `data-generator/` — repeatable synthetic data setup for performance environments.

This directory currently contains placeholders only. Workload shares and thresholds in the design docs are provisional and must be calibrated before use. Performance reports belong under `docs/reports/perf/` per LLD-14 §6.4.
