#!/usr/bin/env bash
# Record the dashboard clips for each theme (default: light dark) on one fresh demo stack under live,
# simulated traffic. Run from anywhere; nothing here needs the shared Docker lock around it, because
# every `docker compose` command is made by scripts/run_dashboard_demo.sh, which takes the lock for that
# one command and no longer.
#   demo/record.sh [light] [dark]
# The stack is the demo project (ai-gateway-demo, ports 4400-4402), never the real one. It starts from
# `down` (so the seeded state is the same every take) and is taken down again afterwards, also when a
# take fails. One traffic stream runs under every take, started WARMUP_S before the first so the 1-hour
# charts have history, so the takes differ in their live numbers and not in anything else.
set -euo pipefail
cd "$(dirname "$0")/.."
RUN=scripts/run_dashboard_demo.sh
WARMUP_S=${WARMUP_S:-100}
TRAFFIC_CALLS=${TRAFFIC_CALLS:-480}
TRAFFIC_S=${TRAFFIC_S:-330}
themes=("$@"); [ ${#themes[@]} -gt 0 ] || themes=(light dark)
for theme in "${themes[@]}"; do
  case "$theme" in light|dark) ;; *) echo "theme must be light or dark, got: $theme" >&2; exit 2 ;; esac
done

# Job control, so the traffic stream (a shell, uv and the simulator) is one process group that cleanup can
# stop whole: killing only the shell would leave the simulator sending at the next stack, with revoked tokens.
set -m
traffic_pid=""
cleanup() {
  [ -z "$traffic_pid" ] || kill -- "-$traffic_pid" 2>/dev/null || true
  $RUN down
}
trap cleanup EXIT

$RUN down
$RUN up
$RUN seed
$RUN traffic --calls "$TRAFFIC_CALLS" --duration "$TRAFFIC_S" > /dev/null &
traffic_pid=$!
echo "== warming up the charts for ${WARMUP_S}s"
sleep "$WARMUP_S"
for theme in "${themes[@]}"; do
  echo "== record $theme"
  THEME=$theme nice -n 19 node demo/record.ts
done
wait "$traffic_pid" || echo "warning: the traffic stream ended with an error, so the clips may show less live traffic" >&2
traffic_pid=""
