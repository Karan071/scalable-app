#!/bin/sh
# Declares exchanges, queues, and bindings (ADR-0003, LLD-08 §2) via the
# management HTTP API, against the real broker credentials, instead of a
# mounted definitions.json -- that approach silently disables RabbitMQ's
# own RABBITMQ_DEFAULT_USER seeding (see git history for the incident).
#
# Idempotent (PUT/POST of identical definitions), but additive: removing a
# queue here does not delete it from a broker that already has it.
#
# RABBITMQ_VHOST selects the vhost: "/" (default) for dev, "scaleforge-test"
# for the isolated test environment (`make test-env`). Everything runs under
# the single shared admin credential pair; per-service RabbitMQ users
# (LLD-08 §2) are a deliberate, already-agreed deferral to a future
# ADR-0003 revisit.
set -eu

: "${RABBITMQ_DEFAULT_USER:?}" "${RABBITMQ_DEFAULT_PASS:?}"
VHOST="${RABBITMQ_VHOST:-/}"
API="${RABBITMQ_API:-http://rabbitmq:15672/api}"

case "$VHOST" in
  /) V="%2F" ;;
  *[!a-z0-9-]*) echo "topology: vhost must be '/' or [a-z0-9-]+, got '$VHOST'" >&2; exit 1 ;;
  *) V="$VHOST" ;;
esac

api() { # api METHOD PATH JSON
  curl -sf -u "$RABBITMQ_DEFAULT_USER:$RABBITMQ_DEFAULT_PASS" -X "$1" "$API$2" \
    -H 'content-type: application/json' -d "$3" >/dev/null
}

# Quorum queue plus its DLQ. DLQ first, since the queue's
# x-dead-letter-routing-key names it (LLD-08 §2).
queue_with_dlq() { # queue_with_dlq NAME
  api PUT "/queues/$V/$1.dlq" '{"durable":true,"arguments":{"x-queue-type":"quorum"}}'
  api PUT "/queues/$V/$1" "{\"durable\":true,\"arguments\":{\"x-queue-type\":\"quorum\",\"x-delivery-limit\":10,\"x-dead-letter-exchange\":\"\",\"x-dead-letter-routing-key\":\"$1.dlq\"}}"
}

# queue_with_dlq plus four dedicated retry-tier queues (LLD-08 §7, E4.5:
# sf_messaging.retry). Dedicated to NAME, not shared across consumers: a
# retry queue's own x-dead-letter-routing-key is a fixed queue argument,
# so it can only ever point back to one original queue -- a message
# lands here via the default exchange (routing_key = "NAME.retry.<tier>",
# sf_messaging.retry.queue_name_for_tier), sits for the tier's own TTL,
# then RabbitMQ's own dead-letter mechanism redelivers it straight back
# to NAME with no consumer-side timer. Opt-in per queue
# (InboxConsumer(..., enable_retry_tiers=True)): every queue still using
# plain queue_with_dlq above is completely unaffected.
queue_with_dlq_and_retries() { # queue_with_dlq_and_retries NAME
  queue_with_dlq "$1"
  api PUT "/queues/$V/$1.retry.1" "{\"durable\":true,\"arguments\":{\"x-queue-type\":\"quorum\",\"x-message-ttl\":5000,\"x-dead-letter-exchange\":\"\",\"x-dead-letter-routing-key\":\"$1\"}}"
  api PUT "/queues/$V/$1.retry.2" "{\"durable\":true,\"arguments\":{\"x-queue-type\":\"quorum\",\"x-message-ttl\":30000,\"x-dead-letter-exchange\":\"\",\"x-dead-letter-routing-key\":\"$1\"}}"
  api PUT "/queues/$V/$1.retry.3" "{\"durable\":true,\"arguments\":{\"x-queue-type\":\"quorum\",\"x-message-ttl\":120000,\"x-dead-letter-exchange\":\"\",\"x-dead-letter-routing-key\":\"$1\"}}"
  api PUT "/queues/$V/$1.retry.4" "{\"durable\":true,\"arguments\":{\"x-queue-type\":\"quorum\",\"x-message-ttl\":600000,\"x-dead-letter-exchange\":\"\",\"x-dead-letter-routing-key\":\"$1\"}}"
}

bind() { # bind EXCHANGE QUEUE ROUTING_KEY
  api POST "/bindings/$V/e/$1/q/$2" "{\"routing_key\":\"$3\"}"
}

api PUT "/vhosts/$V" '{}'
api PUT "/permissions/$V/$RABBITMQ_DEFAULT_USER" '{"configure":".*","write":".*","read":".*"}'

# Exchanges (ADR-0003): sf.events carries facts, sf.commands carries
# requests with exactly one intended consumer.
api PUT "/exchanges/$V/sf.events" '{"type":"topic","durable":true}'
api PUT "/exchanges/$V/sf.commands" '{"type":"topic","durable":true}'

# q.authz.identity (E2.8): authz-svc's projections (LLD-08 §2.1). Retry
# tiers (E4.5, rolled out past the one pilot consumer): a policy-table
# write can hit the same deadlock/serialization class sf_db.retry_on_transient
# already retries locally, just past that budget.
queue_with_dlq_and_retries q.authz.identity
for key in "membership.*.v1" "organization.*.v1" "user.created.v1" "user.disabled.v1" "user.reactivated.v1"; do
  bind sf.events q.authz.identity "$key"
done

# q.audit.ingest (E2.9): audit-svc consumes audit.recorded.v1. Retry tiers
# (E4.5): the chain-hash insert takes an advisory lock per write, a
# transient-contention class worth a few redelivery attempts before DLQ.
queue_with_dlq_and_retries q.audit.ingest
bind sf.events q.audit.ingest audit.recorded.v1

# q.bff.identity (E2.12): the BFF revokes a disabled user's sessions (LLD-01 §8).
queue_with_dlq q.bff.identity
bind sf.events q.bff.identity user.disabled.v1

# q.bff.sse (E4.7, LLD-06 §10): any BFF replica fans order.status_changed
# out to redis-cache pub/sub, keyed by customer_id, for whichever replica
# holds that user's open SSE stream. notification.in_app.v1 (the LLD's
# other fan-out source) isn't bound yet -- notifications-svc, its only
# producer, doesn't exist.
queue_with_dlq q.bff.sse
bind sf.events q.bff.sse order.status_changed.v1

# q.orgs.users (E2.8, LLD-08 §2.1): orgs-svc disables a disabled user's
# memberships in every org. Retry tiers (E4.5): a multi-row membership
# update is the same deadlock-class write this epic already retries
# elsewhere.
queue_with_dlq_and_retries q.orgs.users
bind sf.events q.orgs.users user.disabled.v1

# q.authz.commands (E2.8, LLD-01 §9): authz-svc self-consumes the MFA group
# sync command to call Keycloak's admin API via its own sf-authz-admin client.
# Retry tiers (E4.5): a Keycloak admin-API call is exactly the kind of
# transient httpx failure sf_messaging.retry's is_transient already
# recognizes generically, not just DB deadlocks.
queue_with_dlq_and_retries q.authz.commands
bind sf.commands q.authz.commands identity.mfa_group_sync_requested.v1

# q.users.commands (E2.8, LLD-01 §11.2-§11.3): users-svc self-consumes the
# retry of a failed synchronous Keycloak sf_uid sync, and the Keycloak
# enable/disable that follows a user's status. Retry tiers (E4.5): this
# queue's whole job is retrying a Keycloak call that already failed once
# synchronously -- a second transient httpx failure here is the expected
# case, not the exception.
queue_with_dlq_and_retries q.users.commands
bind sf.commands q.users.commands user.idp_sync_requested.v1
bind sf.commands q.users.commands user.idp_status_sync_requested.v1

# q.catalog.orgs (E3.1, LLD-05 §2): catalog-svc's store_pricing_settings
# projection off organization.* events (currency, shipping, tax, status) --
# how the pricing quote learns an org's settings without calling orgs-svc
# synchronously. Retry tiers (E4.5): an upsert here under concurrent
# organization.* delivery is the same transient-write class as the pilot
# consumer.
queue_with_dlq_and_retries q.catalog.orgs
bind sf.events q.catalog.orgs organization.*.v1

# q.search.catalog (E3.2, LLD-05 §7.2): search-svc indexes catalog-svc's
# product lifecycle (version-guarded upsert/tombstone). Retry tiers
# (E4.5): a version-guarded upsert competing with its own search index's
# other writers under load is the same transient-write class as the
# pilot consumer.
queue_with_dlq_and_retries q.search.catalog
bind sf.events q.search.catalog product.updated.v1
bind sf.events q.search.catalog product.deleted.v1

# q.inventory.catalog (E3.4, LLD-07 §3): inventory-svc creates a zero-stock
# row for every new SKU off catalog-svc's product lifecycle. Retry tiers
# (E4.5): a concurrent zero-stock-row insert can deadlock under load same
# as any other multi-writer insert this service already retries at the DB
# layer (sf_db.retry_on_transient) -- this is the same class of failure,
# just past that local retry budget, LLD-08 §7's own "transient" row.
queue_with_dlq_and_retries q.inventory.catalog
bind sf.events q.inventory.catalog product.updated.v1

# q.inventory.commands (E3.4, LLD-07 §4.2-§4.4): the async alternative to
# the direct internal commit/release/restock calls. orders-svc (E3.5) uses
# the direct calls today; this queue is what it would use instead once it
# prefers a command over a synchronous one. Retry tiers (E4.5): commit/
# release/restock are the same stock-row writers q.inventory.catalog's
# own pilot already covers for the projection path; this is that same
# class on the command path.
queue_with_dlq_and_retries q.inventory.commands
bind sf.commands q.inventory.commands inventory.commit_requested.v1
bind sf.commands q.inventory.commands inventory.release_requested.v1
bind sf.commands q.inventory.commands inventory.restock_requested.v1

# q.orders.inventory (E3.5, LLD-06 §5.1 steps 14-15): the checkout saga's
# async continuation past COMMITTING -- real today, inventory-svc exists
# and publishes both of these. Retry tiers (E4.5): a saga state transition
# landing on the same order row other writers are touching is the same
# transient-contention class, on the path that actually settles an order.
queue_with_dlq_and_retries q.orders.inventory
bind sf.events q.orders.inventory inventory.committed.v1
bind sf.events q.orders.inventory inventory.reservation_expired.v1

# q.orders.payments (E3.5/E3.6, LLD-06 §7): the saga's payment-resolution
# events, plus payment_status-only updates (voided/refunded) for the
# compensation paths that don't drive the saga itself.
queue_with_dlq q.orders.payments
bind sf.events q.orders.payments payment.authorized.v1
bind sf.events q.orders.payments payment.failed.v1
bind sf.events q.orders.payments payment.succeeded.v1
bind sf.events q.orders.payments payment.voided.v1
bind sf.events q.orders.payments payment.refunded.v1

# q.payments.commands (E3.5/E3.6, LLD-06 §5.1/§6): the three commands
# orders-svc's checkout saga and order admin ops emit.
queue_with_dlq q.payments.commands
bind sf.commands q.payments.commands payment.capture_requested.v1
bind sf.commands q.payments.commands payment.refund_requested.v1
bind sf.commands q.payments.commands payment.void_requested.v1

echo "topology: vhost '$VHOST' declared"
