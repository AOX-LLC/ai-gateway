#!/usr/bin/env bash
# Prove that each MCP server container can reach Postgres and nothing outside the stack:
# not the Internet, and not the host machine's own services either.
#
# From inside every server it checks that Postgres answers, that two public addresses cannot
# be connected to, that an outside name does not resolve, and that a throwaway listener on
# the host (bound to 0.0.0.0, as any careless host service would be) cannot be reached at any
# of the host's addresses, the Compose bridge's included. A control runs the same probe in
# the gateway container, which is allowed out and sits on the ordinary `edge` bridge: it must
# reach the Internet and the listener, which shows the probe can tell a blocked route from a
# working one. Without Internet access on the machine the control is inconclusive, and the
# check says so; in CI (CI=true) that is a failure.
# The stack must be up (docker compose up -d --wait).
set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT=ai-gateway

network_gateway() {
  docker network inspect "$(docker network ls -q \
    --filter "label=com.docker.compose.project=$PROJECT" \
    --filter "label=com.docker.compose.network=$1")" \
    --format '{{(index .IPAM.Config 0).Subnet}} {{(index .IPAM.Config 0).Gateway}}'
}

# The first address of a subnet (a bridge's usual host address), whether or not it is assigned.
first_address() {
  python3 -c 'import ipaddress, sys; print(next(ipaddress.ip_network(sys.argv[1]).hosts()))' "$1"
}

LISTEN_PORT=$(python3 -c 'import socket; s = socket.socket(); s.bind(("0.0.0.0", 0)); print(s.getsockname()[1])')
SERVED_DIR=$(mktemp -d)
python3 -m http.server "$LISTEN_PORT" --bind 0.0.0.0 --directory "$SERVED_DIR" >/dev/null 2>&1 &
LISTENER=$!
trap 'kill "$LISTENER" 2>/dev/null || true; rmdir "$SERVED_DIR"' EXIT
sleep 1

read -r backend_subnet _ <<<"$(network_gateway backend)"
read -r edge_subnet _ <<<"$(network_gateway edge)"
host_addresses="$(first_address "$backend_subnet") $(hostname -I)"
control_addresses="$(first_address "$edge_subnet")"

probe() {
  local service=$1 addresses=$2
  docker compose exec -T "$service" python - "$LISTEN_PORT" $addresses <<'PY'
import socket
import sys


def connects(host: str, port: int) -> bool:
    try:
        socket.create_connection((host, port), timeout=3).close()
    except OSError:
        return False
    return True


def resolves(name: str) -> bool:
    try:
        socket.getaddrinfo(name, 443)
    except OSError:
        return False
    return True


port, addresses = int(sys.argv[1]), sys.argv[2:]
print("postgres", connects("postgres", 5432))
print("public-address-1", connects("1.1.1.1", 443))
print("public-address-2", connects("8.8.8.8", 53))
print("outside-name", resolves("example.com"))
print("host-listener", any(connects(address, port) for address in addresses))
PY
}

failed=0
for server in ticketing crm handbook; do
  result=$(probe "$server" "$host_addresses")
  echo "$server: $(tr '\n' ' ' <<<"$result")"
  grep -qx "postgres True" <<<"$result" || { echo "  FAIL: cannot reach postgres"; failed=1; }
  for probe_name in public-address-1 public-address-2 outside-name host-listener; do
    grep -qx "$probe_name False" <<<"$result" || { echo "  FAIL: $probe_name is reachable"; failed=1; }
  done
  published=$(docker compose ps "$server" --format json | python3 -c '
import json, sys

for line in sys.stdin:
    for publisher in json.loads(line)["Publishers"] or []:
        if publisher["PublishedPort"]:
            print(publisher["PublishedPort"])
')
  if [ -n "$published" ]; then
    echo "  FAIL: $server publishes port(s) on the host: $(tr '\n' ' ' <<<"$published")"
    failed=1
  fi
done

control=$(probe gateway "$control_addresses")
echo "gateway (control): $(tr '\n' ' ' <<<"$control")"
if ! grep -qx "host-listener True" <<<"$control"; then
  echo "FAIL: the control could not reach the host listener, so the host check proves nothing"
  failed=1
fi
if grep -qx "public-address-1 True" <<<"$control"; then
  echo "control: the gateway reaches the Internet, so the probe can tell the difference"
elif [ -n "${CI:-}" ]; then
  echo "FAIL: the control could not reach the Internet, so the check proves nothing"
  failed=1
else
  echo "INCONCLUSIVE: the control could not reach the Internet either (no Internet on this machine?)"
fi

[ "$failed" -eq 0 ] && echo "ok: the servers reach Postgres and nothing else"
exit "$failed"
