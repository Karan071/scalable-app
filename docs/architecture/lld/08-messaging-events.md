# LLD-08: Messaging, Events, Outbox/Inbox, Retry and DLQ

| Field | Value |
| --- | --- |
| Status | Draft v0.1 |
| Milestone | M2 (exchange, outbox, relay, inbox, identity events) → M4 (retry tiers, DLQ tooling, saga commands, replay) |
| Components | RabbitMQ cluster, `sf-messaging` library, every publishing/consuming service |
| ADRs | 0003, 0011 |

## 1. Scope

This LLD covers:

- broker topology;
- the message envelope;
- the event and command catalog;
- the transactional outbox and relay;
- idempotent consumers (inbox);
- retry, dead-lettering, and replay;
- schema versioning;
- ordering;
- trace propagation;
- outage behavior.

## 2. Topology

```mermaid
flowchart LR
  subgraph Producer[Producer service]
    TX[(Business tx<br/>+ outbox row)] --> RELAY[Outbox relay]
  end
  RELAY -- publisher confirms --> EX{{sf.events<br/>topic}}
  RELAY --> CX{{sf.commands<br/>topic}}
  EX -- binding: order.*.v1 --> Q[q.notifications.orders<br/>quorum]
  CX -- binding: inventory.release_requested.v1 --> Q2[q.inventory.commands<br/>quorum]
  Q --> C[Consumer]
  C -- transient failure --> R1[q.notifications.orders.retry.1<br/>TTL 5s]
  C -- --> R2[….retry.2 TTL 30s] 
  C -- --> R3[….retry.3 TTL 2m]
  C -- --> R4[….retry.4 TTL 10m]
  R1 & R2 & R3 & R4 -- dead-letter to default exchange,<br/>routing key = q.notifications.orders --> Q
  C -- permanent / exhausted --> DLQ[q.notifications.orders.dlq]
  Q -- x-delivery-limit exceeded --> DLQ
```

| Element | Definition |
| --- | --- |
| vhost | `scaleforge` |
| Exchanges | `sf.events` (topic, durable): facts. `sf.commands` (topic, durable): requests with one intended consumer. |
| Routing key | `<event_type>.v<major>`, e.g. `order.created.v1` |
| Consumer queue | `q.<service>.<purpose>`, quorum, `x-delivery-limit = 10` (crash-loop safety net), `x-dead-letter-exchange = ""`, `x-dead-letter-routing-key = <queue>.dlq`, `x-dead-letter-strategy = at-least-once` |
| Retry queues | `<queue>.retry.<n>`, quorum, `x-message-ttl` per tier, dead-lettering back to `<queue>` through the default exchange |
| DLQ | `<queue>.dlq`, quorum, no TTL. Alerting on depth. |
| Declaration | Topology-as-code: RabbitMQ Messaging Topology Operator CRs (`Exchange`, `Queue`, `Binding`, `Policy`, `User`, `Permission`) in Kubernetes; `definitions.json` in Compose. Services do a **passive declare** at startup and fail readiness if their topology is missing. |
| Access | One RabbitMQ user per service. Permissions: write to `sf.events`/`sf.commands` and own retry/DLQ queues; read own queues only; no configure in Kubernetes. TLS in staging. |

### 2.1 Bindings (initial)

| Queue | Bindings |
| --- | --- |
| `q.authz.identity` | `membership.*.v1`, `organization.*.v1`, `user.created.v1`, `user.disabled.v1`, `user.reactivated.v1` |
| `q.orgs.users` | `user.disabled.v1` |
| `q.audit.ingest` | `audit.recorded.v1` |
| `q.bff.sessions` | `user.disabled.v1` |
| `q.bff.realtime` | `order.status_changed.v1`, `notification.in_app.v1` |
| `q.search.catalog` | `product.updated.v1`, `product.deleted.v1`, `inventory.availability_changed.v1` |
| `q.inventory.catalog` | `product.updated.v1` |
| `q.inventory.commands` | `inventory.commit_requested.v1`, `inventory.release_requested.v1`, `inventory.restock_requested.v1` |
| `q.orders.saga` | `payment.*.v1`, `inventory.committed.v1`, `inventory.released.v1`, `inventory.reservation_expired.v1`, `inventory.restocked.v1` |
| `q.payments.commands` | `payment.capture_requested.v1`, `payment.void_requested.v1`, `payment.refund_requested.v1` |
| `q.cart.orders` | `order.confirmed.v1` |
| `q.users.commands` | `user.idp_sync_requested.v1`, `user.idp_status_sync_requested.v1` |
| `q.authz.commands` | `identity.mfa_group_sync_requested.v1` (self-consumed, LLD-01 §9; correction — not `q.users.commands` as this table previously had it) |
| `q.notifications.requests` | `notification.requested.v1`, `invitation.created.v1` |
| `q.catalog.orgs` | `organization.*.v1` (store pricing settings projection) |

## 3. Envelope

The body is structured JSON following CloudEvents 1.0 attribute names. Extensions are lowercase, per the CloudEvents rules.

```json
{
  "specversion": "1.0",
  "id": "0192f1c4-…",                      // UUIDv7, = outbox id, = AMQP message_id
  "type": "order.created",
  "source": "orders-svc",
  "subject": "0192f1c3-…",                 // aggregate id
  "time": "2026-10-17T12:00:00.123Z",       // producer DB commit time
  "datacontenttype": "application/json",
  "dataschema": "https://schemas.scaleforge.dev/events/order.created/v1.json",
  "sfschemaversion": "1.2",                 // major.minor
  "sfaggregatetype": "order",
  "sfaggregateversion": 3,
  "sforgid": "0191…",                       // tenant, nullable for platform/customer events
  "sfcorrelationid": "…",
  "sfcausationid": "…",                     // id of the message/request that caused this
  "traceparent": "00-…-…-01",
  "tracestate": "",
  "data": { }
}
```

- The AMQP properties mirror `message_id = id`, `content_type`, `type`, and headers `traceparent`/`tracestate`, so that broker-level instrumentation works. `delivery_mode = 2` (persistent).
- **Size limit:** 256 KiB. Larger payloads use a reference (fetch by API) instead of embedding.
- **No secrets in `data`.** The single exception is the encrypted field `invitation.created.token_enc` (LLD-03 §6.3).

## 4. Catalog

These are the PRD §6.2 candidates (marked ●) plus the additions this design needs. Each has a JSON Schema at `contracts/events/<type>/v1.json`.

### 4.1 Events

| Type | Producer | Consumers | Key `data` fields |
| --- | --- | --- | --- |
| ● `organization.created` | orgs | authz, catalog | org_id, slug, name, status |
| `organization.updated` / `.suspended` / `.reactivated` / `.archived` | orgs | authz, catalog | org_id, changed fields / reason_code |
| ● `membership.created` | orgs | authz | membership_id, org_id, user_id, roles_requested[], invited_by |
| `membership.disabled` / `.reactivated` / `.revoked` | orgs | authz | membership_id, org_id, user_id, actor_id |
| ● `membership.role_changed` | authz | audit (via audit.recorded), notifications (optional) | org_id, user_id, role_id, change (granted, revoked, suspended) |
| `invitation.created` | orgs | notifications | invitation_id, org_id, email, org_name, expires_at, token_enc |
| `user.created` / `.updated` / `.disabled` / `.reactivated` | users | authz, bff, orgs | user_id, status |
| ● `product.updated` | catalog | search, inventory | full snapshot (LLD-05 §10) |
| `product.deleted` | catalog | search | product_id, version |
| `price.changed` | catalog | (analytics) | sku_id, old, new |
| ● `inventory.reserved` | inventory | (observability, future) | order_id, reservation_id, lines, expires_at |
| ● `inventory.released` | inventory | orders | order_id, reason |
| `inventory.committed` / `.reservation_expired` / `.restocked` | inventory | orders | order_id |
| `inventory.availability_changed` | inventory | search | sku_id, product_id, in_stock, low_stock |
| ● `order.created` | orders | (analytics, audit) | order_id, org_id, customer_id, total, currency |
| `order.confirmed` | orders | cart, notifications | order_id, lines (sku_id, qty) |
| ● `order.cancelled` | orders | notifications | order_id, reason |
| `order.status_changed` | orders | bff (SSE) | order_id, customer_id, from, to |
| `payment.authorized` | payments | orders | order_id, payment_id, amount |
| ● `payment.succeeded` (captured) | payments | orders | order_id, payment_id, amount |
| ● `payment.failed` | payments | orders | order_id, payment_id, code (declined, expired, error) |
| `payment.voided` / `payment.refunded` | payments | orders | order_id, payment_id, amount |
| ● `notification.requested` | orders (others later) | notifications | user_id, template, params, dedupe_key |
| `notification.in_app` | notifications | bff | user_id, notification_id, title |
| `audit.recorded` | all | audit | LLD-04 §2.1 |

### 4.2 Commands (`sf.commands`)

| Type | Sender | Handler |
| --- | --- | --- |
| `inventory.commit_requested` / `.release_requested` / `.restock_requested` | orders | inventory |
| `payment.capture_requested` / `.void_requested` / `.refund_requested` | orders | payments |
| `user.idp_sync_requested` | users | users (self, async Keycloak sync) |
| `user.idp_status_sync_requested` | users | users (self; on disable/reactivate, sets the Keycloak user's enabled flag to the user's current status and, when disabling, ends their Keycloak sessions — LLD-01 §11.3) |
| `identity.mfa_group_sync_requested` | authz | authz (self, Keycloak `mfa-required` group membership via its own `sf-authz-admin` client — LLD-01 §9, accepted; this row previously said "handler: users", which conflicted with that already-accepted text) |

Commands are idempotent by `(subject, type)` plus the business key. The handler emits the resulting event, e.g. `inventory.released`.

## 5. Transactional outbox

### 5.1 Table (in each producing schema)

| Column | Notes |
| --- | --- |
| `id uuid PK` | The event ID (UUIDv7) |
| `exchange`, `routing_key` | |
| `aggregate_type`, `aggregate_id`, `aggregate_version` | |
| `payload jsonb` | Full envelope |
| `occurred_at timestamptz` | `now()` in the producing transaction |
| `published_at timestamptz NULL` | |
| `attempts int`, `last_error text` | |

Partial index `(occurred_at, id) WHERE published_at IS NULL`.

### 5.2 Writer rule

- The outbox row is inserted **in the same transaction** as the state change, through `sf-messaging`'s `outbox.add(event)`. The library also emits `NOTIFY outbox_<schema>`.
- Publishing directly to the broker from request handlers is **forbidden**, except for the documented BFF audit case (LLD-04 §2.4).

### 5.3 Relay

Runs in every replica of the producing service:

```text
loop:
  wait for NOTIFY or 250 ms poll tick
  BEGIN
    rows = SELECT … WHERE published_at IS NULL ORDER BY attempts, occurred_at, id
           LIMIT 200 FOR UPDATE SKIP LOCKED
    publish each (mandatory=true) ; await publisher confirms (timeout 5 s)
    UPDATE outbox SET published_at = now() WHERE id IN (confirmed ids + returned events)
    UPDATE outbox SET attempts = attempts + 1, last_error = … WHERE id IN (nacked/returned commands)
  COMMIT
  on broker error: exponential backoff 1 s → 30 s (full jitter)
```

- The relay uses a **direct, non-pooled connection**, because `LISTEN` needs a session.
- **Claim order puts `attempts` first**, so rows that keep failing sink behind fresh ones. Ordering by `occurred_at` alone let one batch of permanently failing rows starve everything newer: orgs-svc's relay published nothing from 2026-10-21 to 10-23, blocked behind unroutable `invitation.*` rows. The partial index's `(occurred_at, id)` order no longer matches the sort; fine at current backlog sizes, revisit with EXP-2 if outbox backlogs grow.
- **Returned (unroutable) messages** are split by exchange (correction to this section's original "always a topology bug" rule):
  - **`sf.events`**: an event with no bound queue yet is ordinary pub/sub, e.g. `invitation.created` before notifications-svc ships in M4. The relay logs a warning and marks it published; it is not retried. Missing bindings stay visible in that warning, unlike `mandatory=false`, which would drop silently.
  - **`sf.commands`**: a command always has exactly one intended consumer, so an unroutable one is a real topology bug. Alert, and keep retrying. This is what caught `identity.mfa_group_sync_requested` being published to the wrong exchange.
- A crash after publish but before commit republishes the same `id`. Consumers dedupe.
- **Latency target:** commit → broker p99 ≤ 1 s (to validate).

### 5.4 Retention and replay

- Published rows are deleted after **7 days** by a daily job (or by dropping daily partitions, if EXP-8 favors it).
- **Replay tool** (`sf-messaging` CLI, run as a Kubernetes Job by `platform_operator`, audited):
  - `outbox-replay --service orders --from <t> --to <t> [--type order.*] [--aggregate <id>]`
  - It republishes with the **original IDs**, so consumers dedupe or apply idempotently.
  - Use cases: broker data loss (FS-4), a new consumer backfill, and recovery after a consumer bug.

## 6. Idempotent consumers (inbox)

`inbox(consumer text, message_id uuid, received_at timestamptz, PK(consumer, message_id))`, retained for 14 days.

```text
on message:
  extract trace context; start consumer span
  validate envelope + data against schema (permanent error if invalid)
  BEGIN
    INSERT INTO inbox (consumer, message_id) ON CONFLICT DO NOTHING
    if conflict: COMMIT; ack; return                          # duplicate
    apply handler (DB writes; may add outbox rows)
  COMMIT
  ack
```

- **Side effects outside the DB** (SMTP, payment provider, Keycloak admin API) cannot be covered by the inbox transaction. They use **business-level idempotency**:
  - a provider idempotency key;
  - a notification `dedupe_key`;
  - Keycloak operations that are naturally idempotent (set attribute, add to group).
- **Projections** additionally apply only newer `sfaggregateversion` (§9).
- **Concurrency:** prefetch 20 per channel and a per-pod handler concurrency of 10 (tunable). Ack only after commit. Implemented in `InboxConsumer` (`handler_concurrency`, default 10); each consumer's own DB pool is sized to match (LLD-09 §2).

## 7. Retry and dead-letter policy

| Error class | Examples | Action |
| --- | --- | --- |
| Transient | Timeouts, connection errors, 5xx from dependencies, `lock_timeout`, deadlock after local retries, 503 | Publish a copy to the next retry tier with header `x-sf-attempt += 1`, then ack the original |
| Permanent | Schema invalid, unknown major version, business precondition that cannot become true (unknown aggregate after 24 h) | Straight to DLQ with metadata, then ack |
| Poison (crash before ack) | Handler crashes the process | Redelivered by the broker; `x-delivery-limit = 10` dead-letters it to the DLQ |

| Tier | Delay | Cumulative |
| --- | --- | --- |
| retry.1 | 5 s | 5 s |
| retry.2 | 30 s | 35 s |
| retry.3 | 2 min | ~2.5 min |
| retry.4 | 10 min | ~12.5 min |
| then | DLQ | |

- **Jitter:** tier TTLs are fixed (queue-level TTL avoids head-of-line blocking from per-message TTL). Jitter comes from consumer-side reprocessing concurrency and prefetch. This is coarse, and documented as a trade-off of ADR-0003.
- **Per-consumer overrides:** `q.audit.ingest` adds a 1 h tier; `q.bff.realtime` has **no retries** (stale real-time events are useless, so drop and log).
- **DLQ metadata headers:** `x-sf-error-class`, `x-sf-error` (sanitized, ≤ 500 chars), `x-sf-attempts`, `x-sf-first-failed-at`, `x-sf-consumer`, `x-sf-original-routing-key`.

### 7.1 DLQ inspection and replay

- Each consuming service exposes `Internal(dlq:*)` endpoints from `sf-messaging`:
  - `GET /internal/v1/dlq/{queue}?limit=`: peek without consuming; uses a get-and-requeue pattern.
  - `POST /internal/v1/dlq/{queue}:replay {message_ids[] | all, reason}`.
  - `POST /internal/v1/dlq/{queue}:discard {message_ids[], reason}`.
- The ops console uses them through BFF platform routes (`platform_operator`, step-up).
- **Replay** republishes to the consumer queue through the default exchange with `x-sf-attempt = 0` and `x-sf-replayed-by`. It is safe because consumers are idempotent.
- Every replay and discard records the audit action `dlq.replayed` / `dlq.discarded`.

## 8. Schema versioning

| Rule | Detail |
| --- | --- |
| Location | `contracts/events/<type>/v<major>.json` (JSON Schema 2020-12). The minor version is in the `sfschemaversion` attribute and the schema `$comment`. |
| Compatible (minor) changes | Add optional fields. Add enum values (consumers must have a default branch). Relax constraints. |
| Breaking (major) changes | Remove or rename a field, change a type or meaning, make a field required, tighten constraints |
| Consumer rule | Tolerant reader: ignore unknown fields; unknown enum values go to a safe default or DLQ as permanent |
| Major migration | Producer **dual-publishes** `vN` and `vN+1` (two outbox rows) → consumers migrate → `vN` deprecated after all bindings move (tracked in the event catalog) → removed |
| CI gates | (1) Schema diff against main: a breaking change without a major bump fails. (2) Producer tests validate emitted payloads. (3) Consumer tests run against fixture messages for every supported version. |

## 9. Ordering and concurrency

- There is **no global or per-aggregate ordering guarantee**. Causes: SKIP LOCKED relays, retries, and multiple consumer instances.
- **Consumer patterns:**
  - **Projections** (search docs, authz projections, store settings) apply only newer versions: `WHERE incoming.version > stored.version`.
  - **State machines** (saga, payments) use guarded transitions. A late or duplicate event is a no-op.
  - **Commands** are idempotent by business key.
- **Single Active Consumer** (supported on quorum queues) is available for a queue that truly needs strict order. It is not used by default because of the throughput cost.
- **Scaling:** consumers scale horizontally (KEDA on queue depth in M5). The prefetch × replicas product is bounded by the DB pool budget (LLD-09 §2).

## 10. Trace and correlation propagation

1. **At outbox write:** inject the *current* trace context (the originating request) into the envelope `traceparent`/`tracestate`, plus the correlation ID.
2. **Relay:** a `publish` span linked to the origin span context. The AMQP headers carry `traceparent`.
3. **Consumer:** extract the context and start a `process` span as a **child** of the producer context, for a continuous trace. Batch-style handlers use span links.
4. **Logs** in handlers include `trace_id`, `correlation_id`, `message_id`, and `consumer`.

## 11. Broker outage and partition behavior

| Situation | Behavior |
| --- | --- |
| Broker fully down | Producers keep committing; outbox backlog grows. Alert on `outbox_oldest_unpublished_age_seconds > 60`. Sync request paths are unaffected. Consumers reconnect with backoff (1 s → 30 s). |
| One of 3 nodes down | Quorum queues keep a majority and stay available |
| Network partition | The minority side loses quorum, so its queues are unavailable (consistency over availability). Clients reconnect to the majority. |
| Broker data loss | Restore the definitions; replay the outbox for the loss window (§5.4); consumers dedupe |

## 12. Requirements covered

PRD §6.1, §6.2, §6.3 (all bullets); PRD §4.4 (async rules, trace propagation); Appendix A (message queues, pub/sub, EDA, DLQ).

## 13. Telemetry

| Metric | Labels |
| --- | --- |
| `outbox_pending` | `service` |
| `outbox_oldest_unpublished_age_seconds` | `service` |
| `outbox_publish_total` | `service`, `result` (confirmed, no_subscriber, returned, nacked, error). `no_subscriber` = an `sf.events` message nothing is bound to yet, marked published (§5.3); `returned` = an unroutable `sf.commands` message, retried |
| `outbox_publish_latency_seconds` | `service` (commit → confirm) |
| `consumer_messages_total` | `consumer`, `type`, `result` (processed, duplicate, retried, dead_lettered). Until M4's retry tiers, every handler failure is `dead_lettered` (nack without requeue) and `retried` never occurs |
| `consumer_processing_duration_seconds` | `consumer`, `type` |
| `consumer_retry_total` | `consumer`, `tier` |
| `queue_depth`, `dlq_depth` | `queue` (from the RabbitMQ Prometheus plugin) |
| `event_end_to_end_latency_seconds` | `type` (consumer commit time − envelope `time`) |

## 14. Tests

| Test | Type |
| --- | --- |
| Outbox atomicity: a business transaction rolled back produces no event; a committed one is always published (kill the relay mid-batch) | Integration |
| Duplicate delivery: the same message twice produces one side effect (all consumers, generated) | Integration |
| Retry tiers: transient failure → retry.1..4 → DLQ, with headers present | Integration |
| Poison message (handler raises a fatal error) → DLQ via the delivery limit | Integration |
| DLQ replay → processed once; replay audited | Integration |
| Out-of-order versions → the projection holds the latest | Integration |
| Broker down for 2 min while traffic runs → no lost events; backlog drains | Resilience (FS-4) |
| Schema compatibility CI gate catches a removed field | CI |
| Trace continuity: one trace ID spans producer request → consumer handler | Integration (Jaeger/Tempo API assertion) |
| Outbox replay after a broker wipe | Recovery |
