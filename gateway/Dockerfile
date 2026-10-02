# syntax=docker/dockerfile:1
# Build context is the repository root (the uv workspace needs every member's pyproject).

FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12.10 /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
COPY pyproject.toml uv.lock ./
COPY gateway/ gateway/
COPY servers/ servers/
RUN uv sync --frozen --no-dev --package ai-gateway --no-editable --python /usr/local/bin/python

FROM python:3.12-slim
# Reported on /healthz. Pass them at build time; outside a git checkout they stay "unknown".
ARG GIT_COMMIT=unknown
ARG GIT_BRANCH=unknown
ENV GATEWAY_GIT_COMMIT=$GIT_COMMIT GATEWAY_GIT_BRANCH=$GIT_BRANCH
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin app
COPY --from=builder /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1
WORKDIR /app
COPY config /app/config
USER 10001
EXPOSE 4401
CMD ["ai-gateway"]
