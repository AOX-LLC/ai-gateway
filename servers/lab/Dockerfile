# syntax=docker/dockerfile:1
# The lab upstream of the red-team scorecard: its own image, built only for the `lab` Compose profile,
# and in none of the product's images. Build context is the repository root (the uv workspace needs
# every member's pyproject).

FROM python:3.12-slim@sha256:dddfd7e07f9d15aeeca61529320492139d21cac7f0070c00609243e51e4e0016 AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12.10@sha256:2bb3ebca0a796a155094a27773d290c4b074572e6107f171d88d086682fd2500 /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
COPY pyproject.toml uv.lock ./
COPY gateway/ gateway/
COPY servers/ servers/
RUN uv sync --frozen --no-dev --package lab-upstream --no-editable --python /usr/local/bin/python

FROM python:3.12-slim@sha256:dddfd7e07f9d15aeeca61529320492139d21cac7f0070c00609243e51e4e0016
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin app
COPY --from=builder /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1
WORKDIR /app

USER 10001
EXPOSE 4413
CMD ["lab-upstream"]
