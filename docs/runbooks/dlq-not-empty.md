# Runbook: DLQNotEmpty / AuditDLQNotEmpty

**Alerts:** `dlq_depth > 0`, for 10 m (ticket); for `q.audit.ingest.dlq`, 1 m (page).
**Rule:** [`deploy/compose/prometheus/rules/messaging.yml`](../../deploy/compose/prometheus/rules/messaging.yml)

## What it means

A consumer gave up on at least one message. Until M4's retry tiers, any
handler exception dead-letters the message immediately (nack without
requeue, LLD-08 §7), so one transient failure is enough to land here.

**`AuditDLQNotEmpty` pages** because each message there is an audit
record that was never stored (LLD-04 §2.3).

## Diagnose

Peek without consuming. `ack_requeue_true` puts each message back:

```sh
set -a; . ./.env; set +a
curl -s -u "$RABBITMQ_DEFAULT_USER:$RABBITMQ_DEFAULT_PASS" \
  -H 'content-type: application/json' \
  -X POST "localhost:15672/api/queues/%2F/q.users.commands.dlq/get" \
  -d '{"count":5,"ackmode":"ack_requeue_true","encoding":"auto"}' \
  | python3 -m json.tool
```

In each message, check:

- `properties.headers.x-death[0].reason`: `rejected` means the consumer
  nacked it (a handler error); `delivery_limit` means the consumer kept
  crashing before acking (poison message, LLD-08 §7).
- `properties.type` and the payload: which event, which aggregate.

Find the matching error in the consumer service's logs (logger
`sf_messaging.consumer`, "handler failed for type=… message_id=…"), and
`consumer_messages_total{result="dead_lettered"}` in Prometheus for the
rate and which consumer.

## Fix

1. Fix the cause (bad deploy, downstream outage, data bug).
2. Reprocess: **no replay tooling exists yet.** The DLQ replay endpoints
   (LLD-08 §7.1) and outbox replay CLI (§5.4) land with E4.5 in M4.
   Until then, messages that must be reprocessed need a manual republish
   to the consumer's queue via the management UI's "Publish message", with
   the original payload and `message_id` so the inbox dedupes.
3. Purge only messages you've confirmed are safe to drop (management UI →
   queue → "Purge Messages").

## Known local-dev noise

Service test suites start real consumers on the shared dev broker, with
Keycloak pointed at an unreachable test host. They dead-letter Keycloak
commands (`user.idp_sync_requested`, `identity.mfa_group_sync_requested`)
into `q.users.commands.dlq` and `q.authz.commands.dlq` on every run, and
can also pick up commands your dev servers produced. Locally, peek first;
if every message is a test artifact, purge.
