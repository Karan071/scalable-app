#!/bin/sh
# Creates (if missing) one application database with every service schema,
# idempotently. Used for the isolated test database (`make test-env`) and to
# rebuild the dev database (`make dev-db-reset`).
#
# Run from the repo root. Cluster-wide pieces are NOT created here: the
# svc_* login roles, their search_path, and authz_reader all come from
# init-schemas.sh at the Postgres volume's first boot, and Postgres roles
# are shared by every database in the cluster.
#
# Usage: deploy/compose/postgres/bootstrap-app-db.sh <database>
set -eu

DB="${1:?usage: bootstrap-app-db.sh <database>}"

# Same list as init-schemas.sh -- keep them in sync.
SERVICES="users orgs authz catalog search cart inventory orders payments notifications audit"

PSQL="docker compose -f deploy/compose/compose.yaml --env-file .env exec -T postgres \
  psql -v ON_ERROR_STOP=1 --username postgres --quiet"

$PSQL --dbname postgres -v db="$DB" <<'SQL'
SELECT format('CREATE DATABASE %I', :'db')
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'db') \gexec
SQL

{
  # Re-runs are expected (every `make test`): skip the "already exists" NOTICEs.
  echo "SET client_min_messages = warning;"
  # Extensions are pre-created by the platform, not by services (LLD-09 §1).
  echo "CREATE EXTENSION IF NOT EXISTS citext;"
  echo "CREATE EXTENSION IF NOT EXISTS pg_trgm;"
  echo "CREATE EXTENSION IF NOT EXISTS btree_gin;"
  echo "CREATE EXTENSION IF NOT EXISTS ltree;"
  for svc in $SERVICES; do
    echo "CREATE SCHEMA IF NOT EXISTS \"${svc}\" AUTHORIZATION \"svc_${svc}\";"
  done
  echo "GRANT USAGE ON SCHEMA authz TO authz_reader;"
} | $PSQL --dbname "$DB"

echo "bootstrap-app-db: ${DB} ready"
