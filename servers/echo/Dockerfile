# syntax=docker/dockerfile:1
# Build context is the repository root (the uv workspace needs every member's pyproject).

FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12.10 /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
COPY pyproject.toml uv.lock ./
COPY gateway/ gateway/
COPY servers/ servers/
RUN uv sync --frozen --no-dev --package echo-server --no-editable --python /usr/local/bin/python

FROM python:3.12-slim
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin app
COPY --from=builder /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1
WORKDIR /app

USER 10001
EXPOSE 8000
CMD ["python", "-m", "echo_server"]
