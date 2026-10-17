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

test: ## Run the Python test suite across the workspace, one service at a time.
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

dev: ## Bring up local infra: Postgres, Redis, Keycloak, RabbitMQ, Mailpit, Jaeger.
	test -f .env || (echo "Missing .env — copy .env.example first" && exit 1)
	$(COMPOSE) up -d

dev-down: ## Stop and remove local infra containers (keeps named volumes).
	$(COMPOSE) down

dev-logs: ## Follow local infra logs.
	$(COMPOSE) logs -f
