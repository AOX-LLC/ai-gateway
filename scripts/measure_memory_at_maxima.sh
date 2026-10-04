#!/usr/bin/env bash
# Sets up the verification stack for scripts/measure_memory_at_maxima.py: tokens, a demo approver, and
# the gateway on the monitor pipeline (so the loads are not throttled). Run from the repository root
# with COMPOSE_PROJECT_NAME=ai-gateway-verify and the stack up, and LAB_AUTO_APPROVE=yes in front (it
# plays the approver; no script sets that switch itself). Prints no secret.
set -euo pipefail
[ "${LAB_AUTO_APPROVE:-}" = "yes" ] || { echo "measure: run it with LAB_AUTO_APPROVE=yes in front" >&2; exit 2; }
. "$(dirname "$0")/mask.sh"
APPROVER_ID=""
trap 'rm -f demo.json; [ -z "$APPROVER_ID" ] || docker compose run --rm -T admin approver-remove "$APPROVER_ID" >/dev/null 2>&1 || true; docker compose up -d --force-recreate --wait gateway >/dev/null 2>&1 || true' EXIT
docker compose run --rm -T admin seed-demo > demo.json
export SUPPORT_TOKEN OPS_TOKEN
SUPPORT_TOKEN=$(python3 -c 'import json; print(json.load(open("demo.json"))["harborline-support-bot"])')
OPS_TOKEN=$(python3 -c 'import json; print(json.load(open("demo.json"))["harborline-ops-bot"])')
mask "$SUPPORT_TOKEN"
mask "$OPS_TOKEN"
ADDED=$(docker compose run --rm -T admin approver-add --name "Harborline memory demo approver (fictional, demo data)")
APPROVER_ID=$(sed -n 's/^id  *//p' <<<"$ADDED")
LOGIN=$(sed -n 's/^login  *//p' <<<"$ADDED")
PASSWORD=$(sed -n 's/^password  *//p' <<<"$ADDED")
mask "$PASSWORD"
POLICY_APPROVER_DATABASE_URL=$(
  BASE=$(sed -n 's/^POLICY_AUDITOR_DATABASE_URL=//p' .env) LOGIN="$LOGIN" PASSWORD="$PASSWORD" \
    python3 -c 'import os; from urllib.parse import quote, urlsplit, urlunsplit
u = urlsplit(os.environ["BASE"])
login, password = quote(os.environ["LOGIN"], safe=""), quote(os.environ["PASSWORD"], safe="")
print(urlunsplit(u._replace(netloc=f"{login}:{password}@{u.hostname}:{u.port}")))'
)
mask "$POLICY_APPROVER_DATABASE_URL"
export POLICY_APPROVER_DATABASE_URL APPROVER_ID
GATEWAY_PIPELINE_FILE_IN_CONTAINER=/app/config/pipeline.monitor.toml \
  docker compose up -d --force-recreate --wait gateway >/dev/null
sleep "${SETTLE_S:-10}"
GATEWAY_PIPELINE_FILE_IN_CONTAINER=/app/config/pipeline.monitor.toml \
  uv run scripts/measure_memory_at_maxima.py
