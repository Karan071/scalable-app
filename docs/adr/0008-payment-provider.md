# ADR-0008: Payment provider interface — mock PSP with authorize/capture and signed webhooks

- **Status:** Accepted (2026-10-17)
- **Date:** 2026-10-17

## Context

FR-CHK-05 requires a mock provider that simulates success, decline, timeout, and duplicate callbacks. External payment operations cannot share a local DB transaction (PRD §2.2). PRD §13.1 requires an interface, callback validation, idempotency, and reconciliation.

## Decision

- **Model:** two-phase **authorize → capture**, plus `void` (before capture) and `refund` (after capture).
  - Authorization happens at checkout.
  - Capture happens after the order is confirmed and inventory is committed (OQ-6).
- **Mock provider:** `mock-payment-provider` is a separate deployable outside the trust boundary. Its API is modelled on common PSP shapes:
  - `POST /v1/payment_intents` (authorize), `/{id}/capture`, `/{id}/cancel`, `/{id}/refunds`, `GET /{id}`;
  - an `Idempotency-Key` header on every POST.
- **Webhooks:**
  - Sent to `POST /webhooks/payments/mock` on payments-svc through the gateway.
  - Header `SF-Signature: t=<unix>,v1=<hex HMAC-SHA256(secret, t + "." + body)>`.
  - 5-minute timestamp tolerance. Dedupe on provider event ID.
- **Scenarios:** selected by magic amounts/card tokens and by an admin-only scenario API in non-production builds (decline, timeout, delayed webhook, duplicate webhook, out-of-order webhook, capture failure).
- **Abstraction:** payments-svc defines a `PaymentProvider` port so a real sandbox PSP could be added later.

## Consequences

- Authorization holds reduce compensation cost: a void is cheaper than a refund.
- Reconciliation must handle `unknown` attempts (timeouts) by querying the provider (LLD-06 §9).
- The webhook secret is managed as a rotated Secret, with dual-secret verification during rotation.
