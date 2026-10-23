# LLD-04: Audit Trail and Notifications

| Field | Value |
| --- | --- |
| Status | Draft v0.1 (audit: M2; notifications: M4) |
| Services | audit-svc (schema `audit`), notifications-svc (schema `notifications`) |
| ADRs | 0003, 0006 |

## 1. Scope

This LLD covers:

- **Audit:** recording, storing, and querying security-sensitive and administrative events (PRD §2.5, FR-ORG-06, SEC-10).
- **Notifications:** asynchronous email and in-app notifications (PRD §4.4).

## 2. Audit

### 2.1 Audit event schema (`audit.recorded.v1` data)

| Field | Type | Notes |
| --- | --- | --- |
| `audit_id` | uuid | Producer-generated. The dedupe key. |
| `occurred_at` | timestamp | Producer DB time at commit |
| `actor` | `{type: user\|service\|system, id, session_ref?}` | `session_ref` = first 16 hex chars of the session key hash, never the session ID |
| `on_behalf_of` | user id? | For service actions in a user's context |
| `domain` | `org:<id>` \| `platform` \| `customer` | |
| `action` | string from the catalog (§2.8) | `membership.disabled`, `role.assigned`, … |
| `target` | `{type, id}` | |
| `result` | `success` \| `denied` \| `failed` | |
| `reason` | string? | User-supplied reason (`X-Access-Reason`) or system reason code |
| `changes` | `{field: {from, to}}` | Allow-listed fields per action only (§2.7) |
| `source` | `{service, version}` | |
| `correlation_id`, `trace_id` | string | |
| `client` | `{ip_prefix, ua_family}` | IP truncated (/24 IPv4, /48 IPv6) |
| `severity` | `info` \| `notice` \| `high` | `high` for platform-role grants, identity-config changes, break-glass access |

### 2.2 Storage

| Table | Design |
| --- | --- |
| `audit_events` | Columns mirror §2.1, plus `recorded_at`, `seq bigserial`, `prev_hash bytea`, and `hash bytea` (`SHA-256(prev_hash ‖ canonical_json(event))`). Indexes: `(domain, occurred_at DESC)`, `(actor_id, occurred_at DESC)`, `(target_type, target_id, occurred_at DESC)`. Declarative range partitioning by month is a candidate if EXP-8 justifies it. |
| `inbox` | Dedupe on `audit_id` |

**Append-only enforcement:**

- The `svc_audit` role has `INSERT` and `SELECT` only. `UPDATE`, `DELETE`, and `TRUNCATE` are revoked.
- A trigger rejects updates.
- Retention deletion runs as a separate `audit_retention` role, from a scheduled job only. When partitioning is used, it drops whole partitions.

**Hash chain:**

- The chain is single-writer: the consumer inserts serially under an advisory lock, which is acceptable at audit volumes.
- A verification job recomputes the chain daily and alerts on a break.
- This is tamper *evidence*, not prevention.

### 2.3 Ingestion

- audit-svc consumes `audit.recorded.v1` from its quorum queue, deduplicates on `audit_id` (inbox), and inserts.
- Failed inserts follow the standard retry and DLQ policy (LLD-08 §7). An audit DLQ alert is **high priority**.

### 2.4 Producers

| Producer | Mechanism | Loss window |
| --- | --- | --- |
| Domain services | Outbox row in the **same transaction** as the audited change | None after commit (outbox retained, relay retries) |
| BFF (login, logout, step-up, session revocation) | Direct publish with publisher confirms. On broker failure, a bounded in-memory buffer (1,000 events) is retried. On overflow or shutdown, the event is written as a structured log line `audit.fallback` and collected by Loki. | Documented: if both the broker and the log pipeline fail |
| Keycloak | Native event store (login events, admin events), retained 30 days. Optional M6: an event-listener SPI forwards to RabbitMQ. | Keycloak retention |
| authz denials | Sampled (LLD-02 §14). Platform-domain denials are always recorded. | By design |

### 2.5 Query API and access

| Endpoint | Authz | Scope |
| --- | --- | --- |
| GET `/v1/orgs/{orgId}/audit-events?action=&actor=&from=&to=&cursor=` | org `audit_log:read` (org_admin) | `domain = org:{orgId}` only |
| GET `/v1/platform/audit-events?…` | platform `audit_log:read`, acr ≥ 2 | All |
| GET `/v1/platform/audit-chain:verify` | super_user | Triggers or reads verification status |

- Queries use keyset pagination over `(occurred_at, seq)` with a maximum window of 92 days per query.
- **Reading audit data is itself audited:** platform queries record `audit.queried` with filter parameters.

### 2.6 Retention

- Hot storage for 400 days (OQ-9), then export to object storage as compressed NDJSON with the chain anchors, then drop.
- Backups follow LLD-13 §5.

### 2.7 Redaction rules

- `changes` is built from a per-action **allow-list** of fields. Example: for `org.updated`, the fields name, slug, shipping, tax, and status.
- Free-form request bodies are never copied.
- **Never recorded:** passwords, tokens, cookies, invitation tokens, payment card data, webhook secrets, encryption keys, or full IP addresses.
- A unit test per producer asserts that the emitted audit payload matches the allow-list.

### 2.8 Action catalog (initial)

| Area | Actions |
| --- | --- |
| Auth | `auth.login.succeeded`, `auth.login.failed`, `auth.logout`, `auth.step_up`, `session.revoked`, `session.revoked_all` |
| Users | `user.provisioned`, `user.disabled`, `user.reactivated`, `user.deletion_requested`, `user.anonymized` |
| Orgs | `org.created`, `org.updated`, `org.suspended`, `org.reactivated`, `org.archived` |
| Membership | `membership.created`, `membership.disabled`, `membership.reactivated`, `membership.revoked` |
| Invitations | `invitation.created`, `invitation.resent`, `invitation.revoked`, `invitation.accepted` |
| Authorization | `role.assigned`, `role.revoked`, `role.suspended`, `role.reactivated` (added: a membership reactivation restoring a suspended grant had no action), `platform_role.assigned` (high), `authz.denied` |
| Commerce admin | `product.created/updated/archived`, `price.changed`, `stock.adjusted`, `order.status_changed` (staff), `order.cancelled` (staff), `refund.created` |
| Platform ops | `dlq.replayed`, `reconciliation.run`, `feature_flag.changed`, `platform.access_reason` (break-glass), `audit.queried` |

## 3. Notifications (M4)

### 3.1 Triggers

| Source event | Notification |
| --- | --- |
| `invitation.created` | Email: invitation link |
| `notification.requested` (from orders-svc) | Order confirmed, cancelled, shipped, refunded (email + in-app) |
| `membership.disabled/revoked` | Email to the member (optional, store setting) |
| `user.disabled` | None to the user (avoid tipping off abuse); platform audit only |

### 3.2 Data model

| Table | Columns |
| --- | --- |
| `notifications` | `id`, `dedupe_key UNIQUE` (e.g. `order:{id}:confirmed`), `user_id`, `channel` (email, in_app), `template`, `template_version`, `params jsonb` (allow-listed), `status` (pending, sent, failed, suppressed), `created_at`, `read_at` (in-app) |
| `deliveries` | `id`, `notification_id`, `attempt`, `provider_message_id`, `status`, `error_code`, `attempted_at` |
| `inbox` | Event dedupe |

### 3.3 Pipeline

1. The consumer receives the event and dedupes by inbox.
2. It resolves the recipient through users-svc `GET /internal/v1/users:batch` (svc token, 1 s timeout, retried).
3. It inserts the `notification` row. `ON CONFLICT (dedupe_key) DO NOTHING` gives the business-level dedupe.
4. **Email:** send by SMTP (Mailpit locally) with a 5 s timeout.
   - Transient failure: the delivery attempt is recorded and the message nacked into the retry tier.
   - Permanent failure (e.g. 5xx invalid recipient): status → failed. No retry.
5. **In-app:** publish `notification.in_app.v1 {user_id, notification_id, title}`. The BFF fans it out to SSE (LLD-06 §10). The list API serves history.

**At-most-once email** is not achievable across SMTP. The `deliveries` row is written before sending and checked on redelivery, which limits duplicates to crash-between-send-and-commit cases. This is documented.

### 3.4 Rendering and safety

- Templates are versioned files rendered with autoescaping (HTML) plus a plain-text alternative.
- Parameters are allow-listed per template. No user-supplied HTML.
- Links are built only from `APP_ORIGIN` plus fixed paths.

### 3.5 APIs

| Endpoint | Authz |
| --- | --- |
| GET `/v1/me/notifications?unread=&cursor=` | self |
| POST `/v1/me/notifications/{id}:read` | self |

## 4. Requirements covered

PRD §2.5 (audit bullet); FR-ORG-06; SEC-10; PRD §4.4 (async notifications, audit fan-out); PRD §6.2 `notification.requested`.

## 5. Telemetry

| Metric | Labels |
| --- | --- |
| `audit_events_ingested_total` | `severity`, `source_service` |
| `audit_ingest_lag_seconds` | histogram (`recorded_at - occurred_at`) |
| `audit_chain_verify_status` | gauge (1 ok, 0 broken) |
| `audit_bff_fallback_total` | counter |
| `notifications_total` | `channel`, `template`, `result` |
| `notification_send_duration_seconds` | `channel` |

## 6. Tests

| Test | Type |
| --- | --- |
| Every audited action (§2.8) emits exactly one event with allow-listed fields only | Integration per producer |
| Duplicate `audit.recorded` delivery → one row | Integration |
| `svc_audit` cannot UPDATE or DELETE (permission denied) | Integration |
| Hash-chain verification detects a manual row modification (run as a superuser in the test DB) | Integration |
| Org admin cannot read another org's audit events | API |
| Duplicate `notification.requested` → one email (Mailpit API assertion) | Integration |
| SMTP down → retries → DLQ after max attempts; replay sends once | Resilience |

## 7. Open items

| Item | Owner |
| --- | --- |
| Keycloak event-listener forwarding (M6 optional) | Identity |
| Confirm the audit retention period (OQ-9) | Product |
