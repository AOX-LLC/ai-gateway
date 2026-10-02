#!/usr/bin/env bash
# Prove that each MCP server container can reach Postgres and nothing outside the stack.
#
# For every server it checks, from inside the container, that Postgres answers, that two
# public addresses cannot be connected to, and that an outside name does not resolve. A
# control runs the same probe in the gateway container, which is allowed out: it shows the
# probe can tell a blocked route from a working one. Without Internet access on the machine
# the control is inconclusive, and the check says so; in CI (CI=true) that is a failure.
# The stack must be up (docker compose up -d --wait).
set -euo pipefail
cd "$(dirname "$0")/.."

probe() {
  docker compose exec -T "$1" python - <<'PY'
import socket


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


print("postgres", connects("postgres", 5432))
print("public-address-1", connects("1.1.1.1", 443))
print("public-address-2", connects("8.8.8.8", 53))
print("outside-name", resolves("example.com"))
PY
}

failed=0
for server in ticketing crm handbook; do
  result=$(probe "$server")
  echo "$server: $(tr '\n' ' ' <<<"$result")"
  grep -qx "postgres True" <<<"$result" || { echo "  FAIL: cannot reach postgres"; failed=1; }
  for probe_name in public-address-1 public-address-2 outside-name; do
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

control=$(probe gateway)
echo "gateway (control): $(tr '\n' ' ' <<<"$control")"
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
