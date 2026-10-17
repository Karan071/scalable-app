# ADR-0011: Checkout saga — orchestration in orders-svc

- **Status:** Proposed
- **Date:** 2026-10-17

## Context

Checkout spans order creation, inventory reservation, payment authorization, confirmation, capture, and compensation (PRD §6.4). The saga must handle:

- payment failure after a successful reservation;
- delayed callbacks;
- duplicate checkout;
- repeated events;
- outages.

## Decision

- **orders-svc orchestrates** the saga using a persisted state machine (`orders.checkout_sagas`).
- **Synchronous steps** (the user waits): cart snapshot, price quote, inventory reservation.
- **Payment authorization** is started synchronously, and its outcome may arrive asynchronously (webhook → event).
- **Post-payment steps** run asynchronously through commands over the outbox: commit inventory, capture payment, clear cart, notify.
- **Compensation is explicit per step:** release the reservation, void the authorization, refund the capture.
- **Recovery:** a saga recovery worker resumes stuck sagas by `next_action_at`.

## Alternatives

| Option | Why not |
| --- | --- |
| Choreography (services react to each other's events) | No single place holds the checkout state. Harder to reason about timeouts and compensation. The PRD asks for explicit states. |
| Fully synchronous checkout | Payment timeouts would block requests and leave ambiguous outcomes |
| Fully asynchronous (202 immediately) | Out-of-stock feedback would be delayed. Synchronous reservation gives an immediate 409. |
| Workflow engine (Temporal) | Strong fit, but a new infrastructure product. The learning goal is to build the saga explicitly. |

## Consequences

- orders-svc becomes the coordination point and must be highly available (T0, ≥ 2 replicas).
- Every participant's operation must be idempotent, keyed by `order_id`.
- State diagram and failure matrix: LLD-06 §5.
