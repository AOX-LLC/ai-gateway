#!/usr/bin/env bash
# The Harborline scenario through the gateway and directly, then simulated traffic, with the
# audit log anchored and verified. Run from the repository root with the stack up. Prints no secret.
set -euo pipefail
. "$(dirname "$0")/mask.sh"
# The demo approver is registered below for this run only, under an id of its own (an id is never
# reused), and removed, login and all, when the run ends.
APPROVER_ID=""
trap 'rm -f demo.json; [ -z "$APPROVER_ID" ] || docker compose run --rm -T admin approver-remove "$APPROVER_ID" >/dev/null 2>&1 || true' EXIT
docker compose run --rm -T admin seed-demo > demo.json
SUPPORT=$(python3 -c 'import json; print(json.load(open("demo.json"))["harborline-support-bot"])')
mask "$SUPPORT"
OPS=$(python3 -c 'import json; print(json.load(open("demo.json"))["harborline-ops-bot"])')
mask "$OPS"
DECOY=$(python3 -c 'import json; print(json.load(open("demo.json"))["harborline-decoy-bot"])')
mask "$DECOY"
# A write waits for a person's approval. This is the fictional demo stack, so a registered demo
# approver approves for it (scripts/auto_approver.py, test tooling that needs the two switches:
# --approve-as below, and LAB_AUTO_APPROVE=yes in the environment, which CI sets on this step and
# nothing else does; run it by hand with LAB_AUTO_APPROVE=yes in front).
# The id is generated and opaque (the login and the mapping are written for good, so no name goes in
# them); the demo approver's name lives in the approvers table only.
ADDED=$(docker compose run --rm -T admin approver-add \
  --name "Harborline demo approver (fictional, demo data)")
APPROVER_ID=$(sed -n 's/^id  *//p' <<<"$ADDED")
LOGIN=$(sed -n 's/^login  *//p' <<<"$ADDED")
PASSWORD=$(sed -n 's/^password  *//p' <<<"$ADDED")
mask "$PASSWORD"
# The same database as the auditor's URL, signed in as the demo approver's login.
POLICY_APPROVER_DATABASE_URL=$(
  BASE=$(sed -n 's/^POLICY_AUDITOR_DATABASE_URL=//p' .env) LOGIN="$LOGIN" PASSWORD="$PASSWORD" \
    python3 -c 'import os; from urllib.parse import quote, urlsplit, urlunsplit
u = urlsplit(os.environ["BASE"])
login, password = quote(os.environ["LOGIN"], safe=""), quote(os.environ["PASSWORD"], safe="")
print(urlunsplit(u._replace(netloc=f"{login}:{password}@{u.hostname}:{u.port}")))'
)
mask "$POLICY_APPROVER_DATABASE_URL"
export POLICY_APPROVER_DATABASE_URL
# The gateway polls the registry every 5 seconds, so the tickets tools can take a
# moment to show up: retry the first check for at most about 60 seconds.
retry() { for _ in $(seq 1 30); do "$@" && return 0; sleep 2; done; "$@"; }
GATEWAY_TOKEN="$SUPPORT" retry uv run scripts/test_client.py \
  --scenario scripts/scenarios/harborline.toml --as harborline-support-bot \
  --approve-as "$APPROVER_ID"
GATEWAY_TOKEN="$OPS" uv run scripts/test_client.py \
  --scenario scripts/scenarios/harborline.toml --as harborline-ops-bot \
  --approve-as "$APPROVER_ID"
# The servers publish no port: the scenario runs inside their network.
scripts/direct_check.sh
# Simulated traffic as both bots: what the gateway stored, read back through the
# dashboard's role, must match what was sent. The reader's URL is the one secret the
# simulator needs; the tokens are passed the way the script reads them.
TELEMETRY_READER_DATABASE_URL=$(sed -n 's/^TELEMETRY_READER_DATABASE_URL=//p' .env)
TELEMETRY_READER_DB_PASSWORD=$(sed -n 's/^TELEMETRY_READER_DB_PASSWORD=//p' .env)
mask "$TELEMETRY_READER_DB_PASSWORD"
mask "$TELEMETRY_READER_DATABASE_URL"
# The audit log's reader, so --verify also checks the audit trail, and the CLI can verify it.
POLICY_AUDITOR_DATABASE_URL=$(sed -n 's/^POLICY_AUDITOR_DATABASE_URL=//p' .env)
POLICY_AUDITOR_DB_PASSWORD=$(sed -n 's/^POLICY_AUDITOR_DB_PASSWORD=//p' .env)
mask "$POLICY_AUDITOR_DB_PASSWORD"
mask "$POLICY_AUDITOR_DATABASE_URL"
export TELEMETRY_READER_DATABASE_URL POLICY_AUDITOR_DATABASE_URL
export SIM_SUPPORT_TOKEN="$SUPPORT" SIM_OPS_TOKEN="$OPS" SIM_DECOY_TOKEN="$DECOY"
# The rate limits and the login throttle count in the gateway's memory, and the traffic plan expects
# them empty: a restarted gateway starts each run from nothing, whatever ran before.
fresh_gateway() {
  docker compose restart gateway > /dev/null
  for _ in $(seq 1 45); do
    curl -sf http://127.0.0.1:4401/healthz > /dev/null && return 0
    sleep 2
  done
  echo "the gateway did not come back" >&2
  return 1
}
fresh_gateway
sleep "${SETTLE_S:-8}"  # the catalogue loads the servers' tools just after the gateway answers
uv run scripts/simulate_traffic.py --calls 200 --verify --approve-as "$APPROVER_ID"
curl -sSf http://127.0.0.1:4401/healthz | python3 -c '
import json, sys
health = json.load(sys.stdin)
print(health["telemetry"], health["audit"])
assert health["telemetry"]["status"] == "ok"
assert health["telemetry"]["dropped_total"] == 0
assert health["telemetry"]["rejected_total"] == 0
assert health["audit"]["status"] == "ok"
assert health["audit"]["dropped_total"] == 0 and health["audit"]["rejected_total"] == 0
'
# An anchor taken now, then more traffic, then the chain and the anchor checked: the
# log must still extend what was anchored.
ANCHORS="${RUNNER_TEMP:-/tmp}/audit-anchors.jsonl"
rm -f "$ANCHORS"
uv run gateway-admin audit-anchor --file "$ANCHORS"
fresh_gateway
sleep "${SETTLE_S:-8}"
uv run scripts/simulate_traffic.py --calls 60 --seed 7 --verify --approve-as "$APPROVER_ID"
uv run gateway-admin audit-verify --anchors "$ANCHORS"
rm -f "$ANCHORS"
