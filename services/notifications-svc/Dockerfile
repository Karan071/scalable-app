# syntax=docker/dockerfile:1
#
# Build context is the repo root (the uv workspace lockfile spans every
# service and lib). TODO: pin the base image by digest once CI/Renovate
# can resolve one (LLD-12 §2); a tag is used locally for now.
FROM python:3.13-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /uvx /usr/local/bin/

WORKDIR /build
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# The whole workspace is copied here (not just this service) because uv's
# workspace discovery globs services/* and libs/py/* and --frozen checks
# that against the lockfile; the final image below still only ships this
# one service's code. See docs/architecture/lld/12-platform-deployment.md §2.
COPY pyproject.toml uv.lock ./
COPY libs/py ./libs/py
COPY services ./services

RUN uv sync --frozen --no-dev --no-editable --package scaleforge-notifications-svc

FROM python:3.13-slim
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin svc

COPY --from=builder /build/.venv /app/.venv
COPY --from=builder /build/services/notifications-svc/app /app/app

ENV PATH="/app/.venv/bin:${PATH}" PYTHONUNBUFFERED=1
WORKDIR /app
USER 10001
EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
