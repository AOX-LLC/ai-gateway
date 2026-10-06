#!/usr/bin/env bash
# One Claude Code run through the gateway, as the fictional support assistant, on the demo stack.
#   demo/real-client/run.sh <take-name> <prompt: realistic | compliant | probe> [model]
# Needs the demo stack up and seeded, and `scripts/run_dashboard_demo.sh tokens` run once (it keeps the
# tokens in the git-ignored .demo/). The token is read into this process's environment for the one command
# and never put on a command line, in the MCP config (which names ${GATEWAY_TOKEN}) or in a log.
#
# Claude Code is started in an empty directory with no built-in tools, no settings files and no skills, and
# with the gateway as its only MCP server, so the run shows what the gateway lets a client do and nothing
# else. Every invocation is appended to demo/out/real-client/runs.log: the clip's honesty depends on saying
# how many runs it took.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
NAME=$1
PROMPT_FILE="$HERE/prompts/$2.txt"
MODEL=${3:-sonnet}
OUT="$ROOT/demo/out/real-client"
mkdir -p "$OUT"

TOKEN=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["harborline-support-bot"])' \
  "$ROOT/.demo/client-tokens.json")
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
echo "$(date -u +%FT%TZ) take=$NAME prompt=$2 model=$MODEL" >> "$OUT/runs.log"

cd "$WORK"
GATEWAY_TOKEN="$TOKEN" nice -n 19 claude -p "$(cat "$PROMPT_FILE")" \
  --mcp-config "$HERE/mcp.json" --strict-mcp-config --tools "" \
  --allowedTools "mcp__harborline__*" --model "$MODEL" --max-turns 20 \
  --output-format stream-json --verbose --no-session-persistence \
  --disable-slash-commands --setting-sources "" < /dev/null \
  > "$OUT/$NAME.jsonl" 2> "$OUT/$NAME.err" || echo "claude exited $?" >&2
echo "wrote $OUT/$NAME.jsonl ($(wc -l < "$OUT/$NAME.jsonl") events)"
