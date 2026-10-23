.PHONY: sync fmt lint typecheck test test-env test-alerts test-system test-revocation-latency migrate dev dev-down dev-logs dev-db-reset

COMPOSE := docker compose -f deploy/compose/compose.yaml --env-file .env

# Isolated test environment on the same Compose servers (`make test-env`).
# Every conftest defaults to these; dev data and queues are never touched.
TEST_DB := scaleforge_test
TEST_VHOST := scaleforge-test

# Every service with at least one Alembic revision.
MIGRATING_SERVICES = $(patsubst services/%/migrations/versions/,%,$(sort $(dir $(wildcard services/*/migrations/versions/*.py))))

sync: ## Install/lock the full uv workspace (services + libs).
	uv sync --all-packages

fmt: ## Format Python with ruff.
	uv run ruff format .

lint: ## Lint Python with ruff.
	uv run ruff check .

typecheck: ## Type-check Python with mypy, one workspace member at a time.
	for d in libs/py/*/src services/*/app; do \
		echo "== mypy $$d =="; \
		uv run mypy "$$d" || exit 1; \
	done

test: test-env ## Run the Python test suite across the workspace, one service at a time.
	# Every service's wheel installs a top-level package literally named
	# `app`. `uv sync --all-packages` puts all 13 into this one shared dev
	# venv, so without PYTHONPATH pointing at the right one first, `import
	# app` resolves to whichever service's `app` the editable-install finder
	# happens to return — confirmed by users-svc's own tests silently
	# importing audit-svc's stub instead, 2026-10-18. Each service's own
	# Docker build is unaffected: it syncs only that one `--package`, so
	# there is never more than one `app` installed in that image.
	uv run --all-packages pytest libs/py/*/tests -q
	for d in services/*/; do \
		if ! find "$$d/tests" -maxdepth 1 -name 'test_*.py' -print -quit 2>/dev/null | grep -q .; then \
			continue; \
		fi; \
		echo "== test $$d =="; \
		PYTHONPATH="$$(pwd)/$$d" uv run pytest "$${d}tests" -q || exit 1; \
	done

test-env: ## Rebuild the isolated test DB and RabbitMQ vhost from scratch (`make test` runs it).
	# Fresh every run: no state leaks between runs, and every migration is
	# re-verified against an empty database each time.
	test -f .env || (echo "Missing .env — copy .env.example first" && exit 1)
	$(COMPOSE) exec -T postgres psql -v ON_ERROR_STOP=1 -U postgres -d postgres --quiet \
		-c "DROP DATABASE IF EXISTS $(TEST_DB) WITH (FORCE)"
	sh deploy/compose/postgres/bootstrap-app-db.sh $(TEST_DB)
	$(MAKE) --no-print-directory migrate DB=$(TEST_DB)
	set -a; . ./.env; set +a; \
	curl -s -o /dev/null -u "$$RABBITMQ_DEFAULT_USER:$$RABBITMQ_DEFAULT_PASS" -X DELETE \
		"localhost:15672/api/vhosts/$(TEST_VHOST)"
	$(COMPOSE) run --rm --no-deps -e RABBITMQ_VHOST=$(TEST_VHOST) rabbitmq-topology

migrate: ## Apply every service's migrations to DB (default: the dev DB, scaleforge).
	set -a; . ./.env; set +a; \
	for svc in $(MIGRATING_SERVICES); do \
		echo "== migrate $$svc -> $(or $(DB),scaleforge) =="; \
		(cd services/$$svc && SF_DB_NAME=$(or $(DB),scaleforge) SF_DB_PASSWORD="$$POSTGRES_APP_PASSWORD" \
			uv run --quiet alembic upgrade head) || exit 1; \
	done

test-system: test-env ## Cross-service system tests: real service processes on the test env (tests/system/).
	uv run --all-packages pytest tests/system -q

test-revocation-latency: test-env ## M2 exit evidence: role-revocation latency vs LLD-02 §8 (slow; TRIALS=200, CONCURRENCY=10).
	uv run --all-packages python tests/system/revocation_latency.py \
		--trials $(or $(TRIALS),200) --concurrency $(or $(CONCURRENCY),10) \
		$(if $(SCENARIOS),--scenarios $(SCENARIOS)) $(if $(REPORT),--report $(REPORT))

test-alerts: ## Check Prometheus rules and run their promtool unit tests (LLD-10 §14).
	docker run --rm --entrypoint promtool -v "$$(pwd)/deploy/compose/prometheus/rules:/rules:ro" \
		-w /rules prom/prometheus:v3.1.0 check rules messaging.yml
	docker run --rm --entrypoint promtool -v "$$(pwd)/deploy/compose/prometheus/rules:/rules:ro" \
		-w /rules prom/prometheus:v3.1.0 test rules messaging.test.yml

dev: ## Bring up local infra: Postgres, Redis, Keycloak, RabbitMQ, Mailpit, Jaeger.
	test -f .env || (echo "Missing .env — copy .env.example first" && exit 1)
	$(COMPOSE) up -d

dev-down: ## Stop and remove local infra containers (keeps named volumes).
	$(COMPOSE) down

dev-logs: ## Follow local infra logs.
	$(COMPOSE) logs -f

dev-db-reset: ## DESTRUCTIVE: back up, then rebuild the dev DB from migrations and empty dev queues/sessions. Needs CONFIRM=yes.
	@test "$(CONFIRM)" = yes || (echo "Refusing: this drops the dev database 'scaleforge'. Re-run with CONFIRM=yes." && exit 1)
	mkdir -p .local/backups
	$(COMPOSE) exec -T postgres pg_dump -U postgres -Fc scaleforge > .local/backups/scaleforge-$$(date +%Y%m%d-%H%M%S).dump
	@echo "Backup written to .local/backups/ (restore: pg_restore -U postgres -d scaleforge --clean <file>)"
	$(COMPOSE) exec -T postgres psql -v ON_ERROR_STOP=1 -U postgres -d postgres -c "DROP DATABASE IF EXISTS scaleforge WITH (FORCE)"
	sh deploy/compose/postgres/bootstrap-app-db.sh scaleforge
	$(MAKE) --no-print-directory migrate DB=scaleforge
	# Dev messages and sessions all reference rows that no longer exist.
	set -a; . ./.env; set +a; \
	for q in $$(curl -sf -u "$$RABBITMQ_DEFAULT_USER:$$RABBITMQ_DEFAULT_PASS" "localhost:15672/api/queues/%2F?columns=name" \
			| python3 -c "import json,sys; print(' '.join(q['name'] for q in json.load(sys.stdin) if q['name'].startswith('q.')))"); do \
		curl -sf -u "$$RABBITMQ_DEFAULT_USER:$$RABBITMQ_DEFAULT_PASS" -X DELETE "localhost:15672/api/queues/%2F/$$q/contents" && echo "purged $$q"; \
	done
	$(COMPOSE) exec -T redis-session redis-cli -n 0 FLUSHDB
	$(COMPOSE) exec -T redis-cache redis-cli -n 0 FLUSHDB
	@echo "Dev DB rebuilt. Restart any running services; dev users are re-provisioned on next login."
