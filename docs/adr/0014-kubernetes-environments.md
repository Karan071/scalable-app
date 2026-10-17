# ADR-0014: Kubernetes environments

- **Status:** Proposed (staging target pending: OQ-7)
- **Date:** 2026-10-17

## Context

PRD §13.1 calls for "local plus repeatable staging" with tooling still to be selected.

## Decision

- **Local and CI:** **kind**, with a pinned node image and a version-controlled cluster config (3 nodes: 1 control plane, 2 workers, so topology spread and pod disruption can be demonstrated).
- **Manifests:**
  - Kustomize for app manifests (`base/` + `overlays/{local,ci,staging}`).
  - Helm only to install third-party operators and charts, with pinned versions and values files in the repo.
- **Bootstrap:** a single `make cluster-up` installs cert-manager, Envoy Gateway, CloudNativePG, the RabbitMQ Cluster Operator, the Keycloak Operator, Redis manifests, MinIO, and the observability stack.
- **Staging (default):** kind on a dedicated VM sized for load tests (to be documented in the test-environment report). A managed cluster (for example EKS, GKE, or AKS) stays optional. If adopted, it adds real WAF/DDoS verification and object storage.
- **GitOps (optional, M5+):** Argo CD for staging, to demonstrate drift detection. It is not required for local.

## Consequences

- Load-test results from kind on a single VM are **not** representative of production capacity. Reports must say so (PRD §10).
- No cloud cost by default.
