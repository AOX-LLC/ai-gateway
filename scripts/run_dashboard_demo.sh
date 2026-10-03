#!/usr/bin/env bash
# The demo stack: start it, seed it with a week of fictional traffic, photograph the dashboard, measure
# its memory, and take it all down. Run from the repository root.
#
#   scripts/run_dashboard_demo.sh up        # build and start the stack (compose.yaml + compose.demo.yaml)
#   scripts/run_dashboard_demo.sh seed      # two demo approvers, a week of telemetry, the approval queue
#   scripts/run_dashboard_demo.sh shots     # sign in, exercise the dashboard, save docs/images/*.png
#   scripts/run_dashboard_demo.sh memory    # measure the dashboard container's memory under load
#   scripts/run_dashboard_demo.sh down      # remove the stack and its data
#   scripts/run_dashboard_demo.sh all       # up, seed, shots
#
# It always uses a Compose project of its own (ai-gateway-demo), so a real stack's data is never touched,
# and a demo-only admin password and session secret made for this run (kept in .demo/, which is git-ignored and removed by `down`).
# Your real admin password hash in .env is not read or changed. If the machine is shared, run this under
# the shared Docker lock: flock ~/portfolio-projects/.locks/docker scripts/run_dashboard_demo.sh all
set -euo pipefail
cd "$(dirname "$0")/.."

# Always the demo project, whatever the shell inherited: `down` removes its volumes.
export COMPOSE_PROJECT_NAME=ai-gateway-demo
STATE=.demo
trap 'rm -f "$STATE/tokens.json"' EXIT
COMPOSE=(docker compose -f compose.yaml -f compose.demo.yaml)

demo_password() {
  mkdir -p "$STATE"
  chmod 700 "$STATE"
  if [ ! -s "$STATE/password" ]; then
    (umask 077; python3 -c 'import secrets; print(secrets.token_urlsafe(18))' > "$STATE/password")
  fi
  if [ ! -s "$STATE/session-secret" ]; then
    (umask 077; python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > "$STATE/session-secret")
  fi
  DEMO_SESSION_SECRET=$(cat "$STATE/session-secret")
  DASHBOARD_PASSWORD=$(cat "$STATE/password")
  DEMO_DASHBOARD_PASSWORD_HASH=$(python3 - <<'PY'
import importlib.util, os
spec = importlib.util.spec_from_file_location("p", "scripts/set_dashboard_password.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
print(module.hash_password(open(".demo/password").read().strip()))
PY
)
  export DASHBOARD_PASSWORD DEMO_DASHBOARD_PASSWORD_HASH DEMO_SESSION_SECRET
}

up() {
  [ -f .env ] || python3 scripts/init_env.py
  demo_password
  export GIT_COMMIT GIT_BRANCH
  GIT_COMMIT=$(git rev-parse --short HEAD) GIT_BRANCH=$(git branch --show-current)
  # A fresh Postgres volume can still be starting when a one-shot first connects: try once more.
  nice -n 19 "${COMPOSE[@]}" up -d --build --wait || nice -n 19 "${COMPOSE[@]}" up -d --wait
}

# A login URL for the host: the auditor's URL with the approver's login and password swapped in.
approver_url() {
  BASE=$(sed -n 's/^POLICY_AUDITOR_DATABASE_URL=//p' .env) LOGIN="$1" PASSWORD="$2" python3 - <<'PY'
import os
from urllib.parse import quote, urlsplit, urlunsplit
u = urlsplit(os.environ["BASE"])
login, password = quote(os.environ["LOGIN"], safe=""), quote(os.environ["PASSWORD"], safe="")
print(urlunsplit(u._replace(netloc=f"{login}:{password}@{u.hostname}:{u.port}")))
PY
}

add_approver() {
  local added login password
  added=$("${COMPOSE[@]}" run --rm -T admin approver-add "$1" --name "$2")
  login=$(sed -n 's/^login  *//p' <<<"$added")
  password=$(sed -n 's/^password  *//p' <<<"$added")
  approver_url "$login" "$password"
}

seed() {
  demo_password
  DEMO_APPROVER_1_URL=$(add_approver dana-kerr "Dana Kerr (fictional)")
  DEMO_APPROVER_2_URL=$(add_approver priya-nair "Priya Nair (fictional)")
  export DEMO_APPROVER_1_URL DEMO_APPROVER_2_URL
  (umask 077; "${COMPOSE[@]}" run --rm -T admin seed-demo > "$STATE/tokens.json")
  nice -n 19 uv run scripts/seed_dashboard_demo.py --tokens-file "$STATE/tokens.json"
}

case "${1:-}" in
  up) up ;;
  seed) seed ;;
  shots) demo_password; nice -n 19 uv run scripts/screenshots.py --out docs/images ;;
  memory) demo_password; nice -n 19 python3 scripts/measure_dashboard_memory.py ;;
  down)
    export DEMO_DASHBOARD_PASSWORD_HASH=unused DEMO_SESSION_SECRET=unused
    "${COMPOSE[@]}" down -v
    rm -rf "$STATE"
    ;;
  all) up; seed; demo_password; nice -n 19 uv run scripts/screenshots.py --out docs/images ;;
  *) sed -n '2,12p' "$0" >&2; exit 2 ;;
esac
