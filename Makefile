.PHONY: sync fmt lint typecheck test dev dev-down dev-logs

COMPOSE := docker compose -f deploy/compose/compose.yaml --env-file .env

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

test: ## Run the Python test suite across the workspace.
	uv run --all-packages pytest

dev: ## Bring up local infra: Postgres, Redis, Keycloak, RabbitMQ, Mailpit, Jaeger.
	test -f .env || (echo "Missing .env — copy .env.example first" && exit 1)
	$(COMPOSE) up -d

dev-down: ## Stop and remove local infra containers (keeps named volumes).
	$(COMPOSE) down

dev-logs: ## Follow local infra logs.
	$(COMPOSE) logs -f
