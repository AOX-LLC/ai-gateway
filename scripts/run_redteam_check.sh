#!/usr/bin/env bash
# The acceptance test of Phase 4: a planted ticket tells an assistant to export every customer, and a
# scripted worst-case client does it. Run from the repository root with the stack up (fictional demo
# data only; LAB_AUTO_APPROVE=yes, which plays the human who approves everything, so it is the layers
# that must stop the attack and not a person). Prints no secret and no customer value. Each run
# makes two honest triages as the helper API client of the 09 demo (one session, and a session for each
# call), which no layer may stop, and the export attack twice: from one session, and with every write
# made from a session of its own.
#
#   run A  every layer enforcing: each call goes as the attack file says, the layer that stopped a
#          call is the one it names (read back from telemetry), and the independent oracle, which reads
#          the ticketing database, finds nothing landed.
#   run B  the layers that can be weakened in monitor mode (config/pipeline.monitor.toml): every call
#          goes through, each records the layers that would have stopped it, and the oracle finds the
#          export landed (which shows it can see one).
#   then   the signed-in dashboard (when DASHBOARD_PASSWORD is set) names egress, canary, classifier
#          and rate_limit as layers that stopped or would have stopped a call.
#
# The gateway is restarted before each run (the rate limits and the egress ledger live in its memory)
# and left on its default pipeline at the end.
set -euo pipefail
. "$(dirname "$0")/mask.sh"
# The switch is the caller's to turn on (this script never sets it): run it with LAB_AUTO_APPROVE=yes in
# front, or, in CI, from the one step that carries it.
[ "${LAB_AUTO_APPROVE:-}" = "yes" ] || {
  echo "run_redteam_check: this plays the approver on the fictional demo stack; run it with LAB_AUTO_APPROVE=yes in front" >&2
  exit 2
}
APPROVER_ID=""
restore_gateway() {
  docker compose up -d --force-recreate --wait gateway > /dev/null 2>&1 || true
}
trap 'rm -f demo.json; [ -z "$APPROVER_ID" ] || docker compose run --rm -T admin approver-remove "$APPROVER_ID" >/dev/null 2>&1 || true; restore_gateway' EXIT
docker compose run --rm -T admin seed-demo > demo.json
SUPPORT=$(python3 -c 'import json; print(json.load(open("demo.json"))["harborline-support-bot"])')
mask "$SUPPORT"
HELPER=$(python3 -c 'import json; print(json.load(open("demo.json"))["harborline-helper-api"])')
mask "$HELPER"
ADDED=$(docker compose run --rm -T admin approver-add \
  --name "Harborline red-team demo approver (fictional, demo data)")
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
export POLICY_APPROVER_DATABASE_URL
# The oracle and the telemetry read-back: the database owner (127.0.0.1:4402 only) and the dashboard's reader.
for key in POSTGRES_USER POSTGRES_PASSWORD POSTGRES_DB TELEMETRY_READER_DATABASE_URL; do
  value=$(sed -n "s/^${key}=//p" .env)
  mask "$value"
  export "$key=$value"
done

wait_for_gateway() {
  for _ in $(seq 1 45); do
    curl -sf http://127.0.0.1:4401/healthz > /dev/null && return 0
    sleep 2
  done
  echo "the gateway did not come back" >&2
  return 1
}
# The tools of the servers load just after the gateway answers, and the gateway polls the registry.
settle() { wait_for_gateway; sleep "${SETTLE_S:-10}"; }

ATTACKS=scripts/redteam/attacks

# Each attack gets a gateway of its own: the rate limits and the egress ledger live in its memory, and
# an attack must start from nothing, whatever ran before it. The client for each is the attack file's.
attack() {  # attack <file> <mode> <token>
  docker compose up -d --force-recreate --wait gateway > /dev/null
  settle
  GATEWAY_TOKEN="$3" uv run scripts/redteam/run_attack.py "$ATTACKS/$1" --mode "$2" \
    --approve-as "$APPROVER_ID"
}
all_attacks() {  # all_attacks <mode>
  attack normal-triage.toml "$1" "$HELPER"
  attack normal-triage-sessions.toml "$1" "$HELPER"
  attack export-every-customer.toml "$1" "$SUPPORT"
  attack export-across-sessions.toml "$1" "$SUPPORT"
}

echo "== run A: every layer enforcing =="
all_attacks enforce

echo "== run B: the layers that can be weakened in monitor mode =="
export GATEWAY_PIPELINE_FILE_IN_CONTAINER=/app/config/pipeline.monitor.toml
all_attacks monitor
unset GATEWAY_PIPELINE_FILE_IN_CONTAINER

if [ -n "${DASHBOARD_PASSWORD:-}" ]; then
  echo "== the signed-in dashboard =="
  python3 scripts/check_dashboard.py --expect-data --expect-layers egress,canary,classifier,rate_limit
else
  echo "(DASHBOARD_PASSWORD is not set: the dashboard check was skipped)"
fi
echo "PASS  the export-every-customer acceptance test"
