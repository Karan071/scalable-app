# Runbook: OutboxLag

**Alert:** `max by (service) (outbox_oldest_unpublished_age_seconds) > 60` for 5 m. Severity: page.
**Rule:** [`deploy/compose/prometheus/rules/messaging.yml`](../../deploy/compose/prometheus/rules/messaging.yml)

## What it means

A service's transactional outbox (LLD-08 §5) has a row that no relay has
published for over a minute. The service's own writes still succeed, but
nothing downstream sees them:

| Producer | What goes stale |
| --- | --- |
| orgs-svc | authz projections: new members get no roles, suspended orgs keep working |
| users-svc | authz `user.*` handling, BFF session revocation on disable |
| authz-svc | Keycloak `mfa-required` group sync; role-change audits |
| any | audit trail (`audit.recorded`) |

## Diagnose

Break the backlog down by why it's stuck. Replace `orgs` with the
alerting service's schema (`orgs`, `users`, `authz`):

```sql
SELECT routing_key, attempts, left(coalesce(last_error, '<none>'), 120) AS last_error, count(*)
FROM orgs.outbox
WHERE published_at IS NULL
GROUP BY 1, 2, 3
ORDER BY 4 DESC;
```

Then match the pattern:

| Pattern | Cause | Fix |
| --- | --- | --- |
| `last_error` mentions connection / channel / timeout | Broker unreachable from the service | Restore RabbitMQ; the relay reconnects and drains on its own |
| `last_error` has `NO_ROUTE` on `sf.commands` rows | A command with no queue bound: topology bug | Re-apply topology (`docker compose ... up rabbitmq-topology`); add the binding to `compose.yaml` if it's missing there |
| `attempts = 0`, `last_error` empty, rows keep piling up | No relay is running for this service | Check the service is up and ready (`/readyz` includes relay health); restart it |
| `last_error` has a nack | Broker refused (disk/memory alarm, queue limit) | Check `rabbitmq-diagnostics alarms`; resolve the alarm |

`NO_ROUTE` on `sf.events` rows should never leave a row stuck: since
2026-10-23 the relay marks events with no subscriber as published
(`outbox_publish_total{result="no_subscriber"}`). If you see that, the
service is running relay code from before that change; restart it.

## After the fix

`outbox_oldest_unpublished_age_seconds` should fall to 0 within a few
relay ticks. Rows republish with their original ids, so consumers dedupe
anything that was already delivered (LLD-08 §5.3).

## History

- **2026-10-21 → 10-23**: orgs-svc published nothing for two days. Its
  oldest 200 unpublished rows were unroutable `invitation.*` events, and
  the relay's claim (`ORDER BY occurred_at`) re-selected them every tick.
  Fixed by claiming `ORDER BY attempts, occurred_at` and treating
  subscriber-less events as published. This alert did not exist yet.
