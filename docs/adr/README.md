# Architecture Decision Records

Each ADR follows this format: Context → Decision → Alternatives → Consequences → Revisit triggers.

- Status values: `Proposed` → `Accepted` / `Rejected` / `Deferred (trigger)` → `Superseded by ADR-nnnn`.
- An accepted ADR is not edited. Changing a decision takes a new ADR that supersedes it.

| ADR | Title | Status | Resolves |
| --- | --- | --- | --- |
| [0001](0001-record-architecture-decisions.md) | Record architecture decisions | Accepted | — |
| [0002](0002-tenancy-model.md) | Tenancy model | Proposed | OQ-1 |
| [0003](0003-message-broker-rabbitmq.md) | Message broker: RabbitMQ | Proposed | PRD §13.1 Message broker |
| [0004](0004-casbin-policy-storage.md) | Casbin policy storage and propagation | Proposed | PRD §13.1 Casbin policy storage |
| [0005](0005-authorization-topology.md) | Authorization topology | Proposed | PRD §13.1 Authorization topology |
| [0006](0006-database-topology.md) | Database topology | Proposed | PRD §13.1 Database topology |
| [0007](0007-search-backend.md) | Search backend | Proposed | PRD §13.1 Search backend |
| [0008](0008-payment-provider.md) | Payment provider interface | Proposed | PRD §13.1 Payment provider |
| [0009](0009-service-identity-and-token-propagation.md) | Service identity and user-context propagation | Proposed | SEC-12, SEC-13 |
| [0010](0010-redis-topology.md) | Redis topology | Proposed | PRD §3.2, §13.2 Redis risk |
| [0011](0011-checkout-saga-orchestration.md) | Checkout saga style | Proposed | PRD §6.4 |
| [0012](0012-edge-gateway.md) | Edge / ingress implementation | Proposed | PRD §4.1 Ingress |
| [0013](0013-telemetry-backends.md) | Telemetry backends | Proposed | PRD §13.1 Telemetry backends |
| [0014](0014-kubernetes-environments.md) | Kubernetes environments | Proposed | PRD §13.1 Kubernetes environment |
| [0015](0015-api-conventions.md) | API conventions | Proposed | PRD §4.4, SEC-11 |
| [0016](0016-deployable-units-and-repo.md) | Deployable units and repository layout | Proposed | PRD §13.2 Operational overhead |

PRD §13.1 items that stay deferred:

| Decision | Status | Trigger to resolve |
| --- | --- | --- |
| SLO/capacity targets | Provisional (PRD §7.4) | M4 baselines (LLD-14 §6) → finalize in M6 (LLD-10 §10) |
| Multi-region | Deferred | Single-region restore drill passes with measured RPO/RTO (LLD-13 §7) |
