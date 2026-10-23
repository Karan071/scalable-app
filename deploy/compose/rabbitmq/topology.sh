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

bind() { # bind EXCHANGE QUEUE ROUTING_KEY
  api POST "/bindings/$V/e/$1/q/$2" "{\"routing_key\":\"$3\"}"
}

api PUT "/vhosts/$V" '{}'
api PUT "/permissions/$V/$RABBITMQ_DEFAULT_USER" '{"configure":".*","write":".*","read":".*"}'

# Exchanges (ADR-0003): sf.events carries facts, sf.commands carries
# requests with exactly one intended consumer.
api PUT "/exchanges/$V/sf.events" '{"type":"topic","durable":true}'
api PUT "/exchanges/$V/sf.commands" '{"type":"topic","durable":true}'

# q.authz.identity (E2.8): authz-svc's projections (LLD-08 §2.1).
queue_with_dlq q.authz.identity
for key in "membership.*.v1" "organization.*.v1" "user.created.v1" "user.disabled.v1" "user.reactivated.v1"; do
  bind sf.events q.authz.identity "$key"
done

# q.audit.ingest (E2.9): audit-svc consumes audit.recorded.v1.
queue_with_dlq q.audit.ingest
bind sf.events q.audit.ingest audit.recorded.v1

# q.bff.identity (E2.12): the BFF revokes a disabled user's sessions (LLD-01 §8).
queue_with_dlq q.bff.identity
bind sf.events q.bff.identity user.disabled.v1

# q.orgs.users (E2.8, LLD-08 §2.1): orgs-svc disables a disabled user's
# memberships in every org.
queue_with_dlq q.orgs.users
bind sf.events q.orgs.users user.disabled.v1

# q.authz.commands (E2.8, LLD-01 §9): authz-svc self-consumes the MFA group
# sync command to call Keycloak's admin API via its own sf-authz-admin client.
queue_with_dlq q.authz.commands
bind sf.commands q.authz.commands identity.mfa_group_sync_requested.v1

# q.users.commands (E2.8, LLD-01 §11.2-§11.3): users-svc self-consumes the
# retry of a failed synchronous Keycloak sf_uid sync, and the Keycloak
# enable/disable that follows a user's status.
queue_with_dlq q.users.commands
bind sf.commands q.users.commands user.idp_sync_requested.v1
bind sf.commands q.users.commands user.idp_status_sync_requested.v1

echo "topology: vhost '$VHOST' declared"
