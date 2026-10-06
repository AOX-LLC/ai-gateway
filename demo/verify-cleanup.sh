#!/usr/bin/env bash
# Proves that record.sh cleans up after itself when it is killed in the middle of a take: starts it, waits
# until its traffic stream is really sending, sends SIGTERM, and checks that nothing is left (no simulator,
# no container, no volume, no .demo directory). Takes a few minutes (it builds and seeds the demo stack
# first). Every docker command is made by scripts/run_dashboard_demo.sh, under the shared lock for that one
# command; this script takes no lock.
#   demo/verify-cleanup.sh
#
# The wait must not match this script's own command line: a plain `pgrep -f simulate_traffic` does (the
# shell that runs it has the words in its arguments), which once made a version of this test wait forever
# for nothing. The [s] in the pattern below is the usual fix: the pattern matches "simulate", the text of
# the pattern itself does not.
set -euo pipefail
cd "$(dirname "$0")/.."
PATTERN='[s]imulate_traffic.py --tokens-file'
LOG=$(mktemp)
BUILD_AND_SEED_TIMEOUT_S=900
POLL_S=5

traffic_processes() { pgrep -f "$PATTERN" | wc -l; }
fail() { echo "FAIL: $*" >&2; tail -5 "$LOG" >&2; exit 1; }

WARMUP_S=60 TRAFFIC_S=300 TRAFFIC_CALLS=300 demo/record.sh dark > "$LOG" 2>&1 &
record_pid=$!

waited=0
until [ "$(traffic_processes)" -gt 0 ]; do
  kill -0 "$record_pid" 2> /dev/null || fail "record.sh ended before its traffic started"
  [ "$waited" -lt "$BUILD_AND_SEED_TIMEOUT_S" ] || fail "the traffic did not start in ${BUILD_AND_SEED_TIMEOUT_S}s"
  sleep "$POLL_S"
  waited=$((waited + POLL_S))
done
echo "traffic is sending ($(traffic_processes) simulator process(es)); sending SIGTERM to record.sh"
kill -TERM "$record_pid"
# Bash runs its trap when the foreground command it is in (the warm-up sleep) ends, so wait for it.
wait "$record_pid" || true

left=0
report() { echo "$1: $2"; [ "$2" = "0" ] || left=1; }
report "simulator processes" "$(traffic_processes)"
report "demo containers" "$(docker ps -a --format '{{.Names}}' | grep -c '^ai-gateway-demo' || true)"
report "demo volumes" "$(docker volume ls --format '{{.Name}}' | grep -c '^ai-gateway-demo' || true)"
report ".demo directories" "$(ls -d .demo 2> /dev/null | wc -l)"
report "session state files" "$(ls demo/out/dark/state.json 2> /dev/null | wc -l)"
rm -f "$LOG"
[ "$left" = "0" ] || { echo "FAIL: something was left behind" >&2; exit 1; }
echo "PASS: a take killed mid-traffic left nothing behind"
