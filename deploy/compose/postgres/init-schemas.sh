#!/bin/sh
# Cluster bootstrap for local Compose (ADR-0006, LLD-12 §3).
# One schema and one login role per service in `scaleforge`; `keycloak` is a
# separate database with its own role. Table-level grants (e.g. authz_reader
# on casbin_rule) land with each service's own Alembic migration, not here.
set -eu

: "${POSTGRES_APP_PASSWORD:?POSTGRES_APP_PASSWORD must be set (see .env.example)}"

SERVICES="users orgs authz catalog search cart inventory orders payments notifications audit"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" -c "CREATE DATABASE keycloak;"

for svc in $SERVICES; do
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-SQL
    CREATE SCHEMA IF NOT EXISTS "${svc}";
    CREATE ROLE "svc_${svc}" LOGIN PASSWORD '${POSTGRES_APP_PASSWORD}';
    ALTER SCHEMA "${svc}" OWNER TO "svc_${svc}";
    ALTER ROLE "svc_${svc}" SET search_path = "${svc}";
SQL
done

# Read-only enforcer role (ADR-0004 §authz_reader). USAGE only; SELECT grants
# on casbin_rule/domain_status/policy_version are set by authz-svc's migration.
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-SQL
  CREATE ROLE authz_reader LOGIN PASSWORD '${POSTGRES_APP_PASSWORD}';
  GRANT USAGE ON SCHEMA authz TO authz_reader;
SQL

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname keycloak <<-SQL
  CREATE ROLE keycloak LOGIN PASSWORD '${POSTGRES_APP_PASSWORD}';
  ALTER DATABASE keycloak OWNER TO keycloak;
SQL
