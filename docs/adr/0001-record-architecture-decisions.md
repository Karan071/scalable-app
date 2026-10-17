# ADR-0001: Record architecture decisions

- **Status:** Accepted
- **Date:** 2026-10-17

## Context

The PRD requires that "technology choices and targets can change through documented ADRs" (§13.3) and that "architecture decisions explain trade-offs" (§11).

## Decision

- Record every decision that changes service boundaries, data ownership, trust boundaries, technology choices, or cross-cutting policy as an ADR in `docs/adr/`.
- Use the format Context → Decision → Alternatives → Consequences → Revisit triggers.
- Number ADRs sequentially and never reuse a number.

## Consequences

- Reviews can trace why a choice was made.
- Superseding a decision is explicit.
- There is a small writing overhead per decision.
