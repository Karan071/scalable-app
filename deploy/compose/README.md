# Local Compose stack — manual smoke test

Step-by-step walkthrough to verify the M2 local infrastructure (`compose.yaml`)
and the Python workspace actually work. Run everything from the **repo root**
unless a step says otherwise. Credentials below are the defaults from
[`.env.example`](../../.env.example) — if you changed any in your own `.env`,
use those instead.

## 0. Prerequisites

- Docker Desktop running.
- [`uv`](https://docs.astral.sh/uv/) installed:
  ```sh
  curl -LsSf https://astral.sh/uv/install.sh | sh
  source $HOME/.local/bin/env
  ```
- A `.env` file:
  ```sh
  cp .env.example .env
  ```

## 1. Python workspace (no containers needed)

```sh
make sync        # creates .venv, installs all 13 services + 4 libs
make lint         # ruff — expect "All checks passed!"
make typecheck    # mypy, one workspace member at a time — expect "Success" for each
make test         # pytest, one service at a time — expect all green
```

If any of these fail, stop here — the container stack doesn't depend on this
passing, but something's wrong with the workspace itself.

## 2. Boot one service by hand (still no Docker)

```sh
uv run uvicorn app.main:app --app-dir services/users-svc --port 8000
```

In another terminal:

```sh
curl -s http://127.0.0.1:8000/livez     # expect {"status":"ok"}
curl -s http://127.0.0.1:8000/readyz    # expect {"status":"ok"}
curl -s -i http://127.0.0.1:8000/nope   # expect HTTP 404, content-type: application/problem+json
```

Stop the server (`Ctrl+C`) when done. This proves the shared FastAPI
template works independent of Compose.

## 3. Bring up the infra stack

```sh
make dev
```

Then check everyone's status:

```sh
docker compose -f deploy/compose/compose.yaml --env-file .env ps
```

Expect `postgres`, `adminer`, `redis-session`, `redis-cache`, `rabbitmq`,
`keycloak` as `Up (healthy)`, and `mailpit`, `jaeger`, `otel-collector`,
`prometheus` as plain `Up` (no healthchecks). `keycloak-config-cli` and `rabbitmq-topology`
are one-shot jobs — they should show `Exited (0)`, not `Up`. Give Keycloak
30–60s before its healthcheck passes; `keycloak-config-cli` won't even start
until it does.

If either one-shot job shows a non-zero exit code:

```sh
docker compose -f deploy/compose/compose.yaml --env-file .env logs keycloak-config-cli
docker compose -f deploy/compose/compose.yaml --env-file .env logs rabbitmq-topology
```

## 4. Postgres

**Schemas and roles** (one schema + one login role per service, ADR-0006):

```sh
docker compose -f deploy/compose/compose.yaml --env-file .env exec postgres \
  psql -U postgres -d scaleforge -c "\dn"
docker compose -f deploy/compose/compose.yaml --env-file .env exec postgres \
  psql -U postgres -d scaleforge -c "\du"
```

Expect 11 schemas (`users`, `orgs`, `authz`, `catalog`, `search`, `cart`,
`inventory`, `orders`, `payments`, `notifications`, `audit`) and roles
`svc_users`, `svc_orgs`, …, plus `authz_reader`.

**Browse visually with Adminer**: open <http://localhost:8082>.

| Field | Value |
| --- | --- |
| System | PostgreSQL (preselected) |
| Server | `postgres` (prefilled) |
| Username | `postgres` |
| Password | `.env`'s `POSTGRES_SUPERUSER_PASSWORD` (default `postgres_local_dev`) |
| Database | `scaleforge` |

Use the schema dropdown in the left sidebar to switch between the 11
schemas. `users` and `orgs` have tables now (run each service's migration
first: `cd services/<svc> && SF_DB_PASSWORD=app_local_dev uv run alembic
upgrade head`); the rest are still empty, no migrations yet. Switch
**Database** to `keycloak` (same server, same login) to see Keycloak's own
tables, which do have data.

To see only one service's own schema (matching what that service's DB role
can actually see), log in as `svc_users` / `.env`'s `POSTGRES_APP_PASSWORD`
(default `app_local_dev`) instead of `postgres`.

## 5. Redis

```sh
docker compose -f deploy/compose/compose.yaml --env-file .env exec redis-session redis-cli ping   # PONG
docker compose -f deploy/compose/compose.yaml --env-file .env exec redis-cache redis-cli ping      # PONG
```

Nothing is stored yet — no session or cache code exists until E2.4.

## 6. RabbitMQ

Open <http://localhost:15672>.

| Field | Value |
| --- | --- |
| Username | `.env`'s `RABBITMQ_DEFAULT_USER` (default `scaleforge`) |
| Password | `.env`'s `RABBITMQ_DEFAULT_PASS` (default `rabbitmq_local_dev`) |

Go to the **Exchanges** tab and confirm `sf.events` and `sf.commands` (type
`topic`, durable) exist. Go to the **Queues** tab and confirm `q.authz.identity`
and `q.authz.identity.dlq` (type quorum) exist, with `q.authz.identity` bound
to `sf.events` on `membership.*.v1`, `organization.*.v1`, `user.created.v1`,
`user.disabled.v1`, and `user.reactivated.v1` (LLD-08 §2.1, E2.8). Also
confirm `q.catalog.orgs`, bound to `organization.*.v1` (E3.1, LLD-05 §2):
catalog-svc's `store_pricing_settings` projection. Also confirm
`q.search.catalog`, bound to `product.updated.v1` and `product.deleted.v1`
(E3.2, LLD-05 §7.2): search-svc's index.

## 7. Mailpit

Open <http://localhost:8025>. No login. Empty inbox is correct — nothing
sends mail yet.

## 8. Telemetry: Collector, Jaeger, Prometheus

Services export OTLP to `localhost:4317`, the SDK default, so they need no
config. That port belongs to `otel-collector` (LLD-10 §5), which forwards
traces to Jaeger and serves metrics for Prometheus to scrape.

| What | Where | Check |
| --- | --- | --- |
| Collector | `:4317` gRPC, `:4318` HTTP | `curl -s localhost:13133/` → `"Server available"` |
| Traces | Jaeger UI <http://localhost:16686> | Services appear in the Service dropdown once they've handled a request |
| Metrics | Prometheus <http://localhost:9090> | Query `outbox_oldest_unpublished_age_seconds` (LLD-08 §11 alerts above 60) |

Alerts (LLD-10 §9) are evaluated by Prometheus. See them at
<http://localhost:9090/alerts>; each one links its runbook in `docs/runbooks/`.
After editing `prometheus/rules/messaging.yml`, run `make test-alerts`.

Metrics export every 60 s by default. Set `OTEL_METRIC_EXPORT_INTERVAL=5000`
on a service to see them sooner. Series carry `job="scaleforge/<service>"`.

Telemetry is never a hard dependency: with the Collector down, services
run normally and exit within a few seconds (each OTLP export gives up
after 2 s).

## 9. Keycloak

Open <http://localhost:8081>.

| Field | Value |
| --- | --- |
| Username | `admin` |
| Password | `.env`'s `KEYCLOAK_ADMIN_PASSWORD` (default `admin_local_dev`) |

After logging in, switch realm (top-left dropdown, currently showing
`master`) to **`scaleforge`**, then check:

- **Clients** → `sf-bff`, `sf-svc-bff`, `sf-svc-users`, `sf-svc-orgs`,
  `sf-svc-authz`, `sf-svc-search`, `sf-users-admin`, `sf-authz-admin` all exist.
- **Groups** → `mfa-required` exists, with realm role `mfa_required`.
- **Realm settings → Tokens** → Access Token Lifespan shows 5 minutes.
- **Authentication** → `sf-browser` is bound as the browser flow (MFA and
  step-up, LLD-01 §3.3).

If any of this is missing, `keycloak-config-cli` didn't apply cleanly —
check its logs (step 3). It skips files it has already applied, so after
editing the realm file re-run it with
`docker compose -f deploy/compose/compose.yaml --env-file .env run --rm --no-deps keycloak-config-cli`.

To see MFA by hand: add a user to `mfa-required` and sign in through the
BFF. The user must enroll an authenticator app, and every step-up
(`/auth/step-up`) asks for a code. `make test-system` checks all of this
automatically (`tests/system/test_bff_login.py`).

## 10. Tests and the isolated test environment

`make test` never touches the dev database, queues, or sessions. It first
runs `make test-env`, which rebuilds a separate environment on the same
servers **from scratch** (so no state leaks between runs, and every
migration is re-checked against an empty database):

| Dev (`make dev`, your services) | Tests (`make test-env`) |
| --- | --- |
| Postgres DB `scaleforge` | Postgres DB `scaleforge_test` |
| RabbitMQ vhost `/` | RabbitMQ vhost `scaleforge-test` (same topology) |
| redis-session / redis-cache DB 0 | DB 1 |

Every test conftest defaults to the right-hand column. Running a single
suite directly (`pytest services/<svc>/tests`) needs `make test-env` once
first. Without it the test DB is missing and the integration tests skip.

**System tests** (`tests/system/`) run real service processes together,
which no per-service suite can: every service's package is named `app`, so
two can't share one interpreter. They use the same test environment plus a
fake OIDC IdP (JWKS and token endpoint) in place of Keycloak, so no
production code is bypassed. `make test-revocation-latency` measures LLD-02
§8's revocation targets and writes a report to `docs/reports/perf/`. It's
slow (minutes) and is not part of `make test`. `make test-system`
runs the pass/fail system tests (e.g. LLD-09 §2 bulkheads under a slow
Keycloak). `test_bff_login.py` is the exception to the fake IdP: it signs
throwaway users in through two BFF replicas against the real Compose
Keycloak (MFA, step-up, logout, admin disable) and deletes them afterwards.
`test_tenant_isolation.py` runs the LLD-14 §4 tenant-isolation matrix. It
generates the matrix from the `x-sf-authz` route metadata in each service's
`/openapi.json`. Service logs go to `.local/system-tests/`.

catalog-svc's and search-svc's own per-service suites (`make test`) cover
most of their own correctness, including a mutation check on search-svc's
out-of-order version guard. `make search-reindex ORG_ID=<uuid>` runs
search-svc's full-reindex job (LLD-05 §9) against whatever catalog-svc and
search-svc you already have running — it needs the `sf-svc-search`
Keycloak client this epic added (`keycloak-config-cli` applies it
automatically; see step 9).

`make migrate` applies every service's migrations to the dev DB
(`make migrate DB=<name>` for another).

**Resetting dev data:** `make dev-db-reset CONFIRM=yes`. It backs up the
dev DB to `.local/backups/` (gitignored), rebuilds `scaleforge` from
migrations, empties the dev queues, and clears dev sessions. The
`keycloak` database is not touched; dev users are re-provisioned on their
next login. Restart running services afterwards.

## 11. Tear down

```sh
make dev-down     # stops containers, keeps named volumes (data persists)
```

To wipe everything including data (schemas, Keycloak realm, RabbitMQ state):

```sh
docker compose -f deploy/compose/compose.yaml --env-file .env down -v
```

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| RabbitMQ login fails with correct-looking credentials | Stale data volume from before a config fix. Recreate it: `docker compose -f deploy/compose/compose.yaml --env-file .env rm -sf rabbitmq && docker volume rm compose_rabbitmq-data && docker compose -f deploy/compose/compose.yaml --env-file .env up -d` |
| `keycloak-config-cli` exits non-zero | Check its logs; usually Keycloak wasn't actually healthy yet, or a client/role in `realm-scaleforge.json` conflicts with existing realm state from a previous run |
| A container shows `unhealthy` forever | `mailpit`, `jaeger`, and `otel-collector` have no healthchecks on purpose (minimal images, nothing depends on them) — if you see `unhealthy` on one of those, you're looking at stale container state; recreate it |
