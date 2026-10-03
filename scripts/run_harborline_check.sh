#!/usr/bin/env bash
# The Harborline scenario through the gateway and directly, then simulated traffic, with the
# audit log anchored and verified. Run from the repository root with the stack up. Prints no secret.
set -euo pipefail
. "$(dirname "$0")/mask.sh"
# The demo approver is registered below for this run only; it is deactivated when the run ends.
trap 'rm -f demo.json; docker compose run --rm -T admin approver-deactivate harborline-approver >/dev/null 2>&1 || true' EXIT
docker compose run --rm -T admin seed-demo > demo.json
SUPPORT=$(python3 -c 'import json; print(json.load(open("demo.json"))["harborline-support-bot"])')
mask "$SUPPORT"
OPS=$(python3 -c 'import json; print(json.load(open("demo.json"))["harborline-ops-bot"])')
mask "$OPS"
# A write waits for a person's approval. This is the fictional demo stack, so a registered demo
# approver approves for it (scripts/auto_approver.py, test tooling that needs the two switches).
docker compose run --rm -T admin approver-add harborline-approver \
  --name "Harborline demo approver (fictional, demo data)" > /dev/null
POLICY_APPROVER_DATABASE_URL=$(sed -n 's/^POLICY_APPROVER_DATABASE_URL=//p' .env)
POLICY_APPROVER_DB_PASSWORD=$(sed -n 's/^POLICY_APPROVER_DB_PASSWORD=//p' .env)
mask "$POLICY_APPROVER_DB_PASSWORD"
mask "$POLICY_APPROVER_DATABASE_URL"
export POLICY_APPROVER_DATABASE_URL LAB_AUTO_APPROVE=yes
# The gateway polls the registry every 5 seconds, so the tickets tools can take a
# moment to show up: retry the first check for at most about 60 seconds.
retry() { for _ in $(seq 1 30); do "$@" && return 0; sleep 2; done; "$@"; }
GATEWAY_TOKEN="$SUPPORT" retry uv run scripts/test_client.py \
  --scenario scripts/scenarios/harborline.toml --as harborline-support-bot \
  --approve-as harborline-approver
GATEWAY_TOKEN="$OPS" uv run scripts/test_client.py \
  --scenario scripts/scenarios/harborline.toml --as harborline-ops-bot \
  --approve-as harborline-approver
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
export SIM_SUPPORT_TOKEN="$SUPPORT" SIM_OPS_TOKEN="$OPS"
uv run scripts/simulate_traffic.py --calls 200 --verify --approve-as harborline-approver
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
uv run scripts/simulate_traffic.py --calls 60 --seed 7 --verify --approve-as harborline-approver
uv run gateway-admin audit-verify --anchors "$ANCHORS"
rm -f "$ANCHORS"
