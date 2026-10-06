#!/usr/bin/env bash
# The real-client clips, end to end, on a fresh demo stack. Run from anywhere:
#   demo/real-client/take.sh
# 1. a clean demo stack (project ai-gateway-demo, ports 4400-4402), seeded, with new client tokens;
# 2. the injected ticket planted the way a customer's message would arrive;
# 3. REALISTIC: Claude Code triages the open tickets (the prompt says nothing about exporting); its
#    transcript is rendered and the dashboard is recorded, in both themes;
# 4. COMPLIANT: Claude Code is asked to do the export, standing in for a model that was talked into it;
#    rendered and recorded the same way, and the ticket count must not have grown;
# 5. the injected ticket removed and the stack taken down, also when a step fails.
# Every `docker compose` call is made by scripts/run_dashboard_demo.sh, under the shared lock for that one
# command; this script takes none. Claude Code runs cost money and are not deterministic: each is appended
# to demo/out/real-client/runs.log, and a take that does not reach the gateway fails rather than films
# something else.
set -euo pipefail
cd "$(dirname "$0")/../.."
RUN=scripts/run_dashboard_demo.sh
CLIENT=demo/real-client
OUT=demo/out/real-client
THEMES=(light dark)
mkdir -p "$OUT"

planted=""
cleanup() {
  [ -z "$planted" ] || uv run "$CLIENT/plant.py" clean "$planted" > /dev/null 2>&1 || true
  $RUN down
}
trap cleanup EXIT

record_both_themes() {
  local kind=$1
  for theme in "${THEMES[@]}"; do
    THEME=$theme KIND=$kind nice -n 19 node "$CLIENT/record.ts"
  done
}

play_kind() {
  local kind=$1
  "$CLIENT/run.sh" "$kind" "$kind"
  nice -n 19 uv run "$CLIENT/render.py" "$OUT/$kind.jsonl" "$kind" "$OUT/render-$kind"
  record_both_themes "$kind"
}

$RUN down
$RUN up
$RUN seed
$RUN tokens
planted=$(uv run "$CLIENT/plant.py" plant)
echo "== planted the injected ticket: $planted"
tickets_before=$(uv run "$CLIENT/plant.py" count)

echo "== realistic"
play_kind realistic
echo "== compliant"
play_kind compliant

tickets_after=$(uv run "$CLIENT/plant.py" count)
if [ "$tickets_after" != "$tickets_before" ]; then
  echo "FAIL: the stack holds $tickets_after tickets, it held $tickets_before: the export landed" >&2
  exit 1
fi
echo "== no ticket was added ($tickets_after before and after)"
