# ADR-0005: Authorization topology — embedded enforcer, centralized administration

- **Status:** Accepted (2026-10-17)
- **Date:** 2026-10-17

## Context

PRD §13.1 asks to compare Casbin embedded in services with a centralized authorization service, on latency, availability, consistency, and complexity.

## Decision

- **Embedded enforcement:** every service embeds the Casbin enforcer through the shared `sf-authz` library and evaluates decisions in-process.
- **Centralized administration:** authz-svc owns policy *administration* (role catalog, assignments, and the APIs for changing them) and publishes changes (ADR-0004).

## Comparison

| Criterion | Embedded (chosen) | Centralized decision service |
| --- | --- | --- |
| Latency | Microseconds, in-process | Network hop per decision (~1–5 ms), or caching that reintroduces staleness |
| Availability | Decisions continue if authz-svc is down; only *changes* stop | authz-svc becomes T0 for every request |
| Consistency | Eventual (bounded by propagation target) | Strong at the decision point |
| Complexity | Shared library versioning; reload logic in every service | Extra hop, client retries, and caching in every service anyway |
| Auditability | Decision logs are distributed (collected via OTel/logs) | Central decision log |

## Consequences

- The policy model (`model.conf`) is versioned with `sf-authz`. Model changes need a coordinated library rollout: add new matchers before policies use them (expand/contract).
- Every service holds the whole policy in memory. This is acceptable at expected sizes.
- The revocation target must be measured per service (LLD-02 §8).

## Revisit triggers

- Policy size or memory per pod becomes significant.
- A requirement for synchronous, strongly consistent revocation.
- Need for non-Python services. A sidecar or central service would then avoid porting the library.
