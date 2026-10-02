#!/usr/bin/env bash
# Run the Harborline scenario directly against each MCP server, without the gateway.
#
# The servers publish no port and sit on an internal Compose network, so the host cannot
# reach them; scripts/test_client.py runs in the direct-check service, which is on that
# network. The stack must be up (docker compose up -d --wait); --no-deps keeps each run from
# starting the setup one-shot again. Run from anywhere.
set -euo pipefail
cd "$(dirname "$0")/.."

for server in ticketing crm handbook; do
  docker compose run --rm -T --no-deps direct-check \
    --scenario /scripts/scenarios/harborline.toml \
    --direct "$server"
done
