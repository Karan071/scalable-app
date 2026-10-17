# ADR-0013: Telemetry backends — Prometheus, Tempo, Loki, Grafana

- **Status:** Accepted (provisional until M6)
- **Date:** 2026-10-17

## Context

The PRD fixes OTel SDKs and the Collector, and Prometheus/Grafana for metrics. The trace backend (Jaeger or Tempo) and the log backend are open (PRD §13.1).

## Decision

| Signal | Backend | Ingestion |
| --- | --- | --- |
| Metrics | Prometheus 3.x | OTLP from the Collector to Prometheus's native OTLP receiver; Kubernetes/node metrics through standard scrape (kube-state-metrics, node-exporter) |
| Traces | Grafana Tempo (single-binary mode) | OTLP from the Collector gateway |
| Logs | Grafana Loki (single-binary mode) | Collector `filelog` receiver (agent DaemonSet) → Loki OTLP endpoint |
| UI | Grafana | Datasources with trace↔log↔metric correlation (exemplars, `trace_id` derived fields) |

- **Compose before M6:** Jaeger all-in-one serves as a lightweight trace viewer in M2–M5. It can be swapped behind the Collector without SDK changes.
- **Retention (initial):**
  - metrics 15 days;
  - traces 7 days;
  - logs 14 days;
  - audit is **not** a telemetry concern and lives in audit-svc.

## Alternatives

| Option | Why not |
| --- | --- |
| Jaeger as the final trace backend | Fine for traces. Tempo gives tighter Grafana correlation and object-storage-backed retention. |
| OpenSearch / ELK for logs | Heavier to operate; not needed |
| Vendor SaaS | Out of scope for a self-contained portfolio |

## Consequences

- One UI (Grafana) for all signals.
- Single-binary modes are not HA. That is acceptable, because the telemetry pipeline must not affect business traffic (PRD §7.1), and the documented loss behavior applies.
