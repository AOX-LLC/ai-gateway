#!/usr/bin/env python3
"""Measure each long-running service's memory at its maxima, and say what limit that implies.

    scripts/measure_memory_at_maxima.sh          # sets the stack up, then runs this

Run against the verification stack only (project `ai-gateway-verify`): the services are recreated
first, so each cgroup's own recorded peak (`memory.peak`, read from inside the container) starts
from a cold start, then these loads run through the gateway as the two demo bots, with the layers
that can be weakened in monitor mode so nothing is throttled before it has done its work:

  1. idle: twenty seconds with nothing to do;
  2. reads: forty concurrent MCP sessions, each paging through every read tool at the largest
     page the servers allow, and the largest handbook documents (the classifier judges each result);
  3. oversize: thirty sessions sending arguments of 60 KiB (just under the size the layers refuse);
  4. approvals: thirty writes at once, each held for five seconds for a person, then approved;
  5. volume: about three thousand quick calls in a minute (telemetry, audit and the buffers).

For each service it prints the peak and the limit that is 1.5 times the peak (rounded up to 16 MiB,
with the floors in compose.yaml). It changes no file. The dashboard is measured by
scripts/measure_dashboard_memory.py, and the one-shot containers (setups, admin, migrate) exit
before a peak can be read: they keep the limits they have. Harborline Supply Co. is fictional.
"""

import contextlib
import math
import os
import subprocess
import sys
import time
from typing import Any

import anyio

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from mcp.shared.exceptions import MCPError

from redteam.scripted_client import connect

PROJECT = "ai-gateway-verify"
URL = "http://127.0.0.1:4401/mcp"
LONG_RUNNING = [
    "postgres",
    "gateway",
    "ticketing",
    "crm",
    "handbook",
    "telemetry-purge",
    "approvals-purge",
]
FLOOR_MIB = {"postgres": 256}
DEFAULT_FLOOR_MIB = 64


def docker(*args: str) -> str:
    done = subprocess.run(  # noqa: S603 - fixed command, arguments from this script
        ["docker", *args],  # noqa: S607 - docker on PATH, as everywhere else here
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout.strip()


def peak_mib(service: str) -> float:
    name = f"{PROJECT}-{service}-1"
    return int(docker("exec", name, "cat", "/sys/fs/cgroup/memory.peak")) / 1024 / 1024


def limit_for(service: str, peak: float) -> int:
    wanted = math.ceil(peak * 1.5 / 16) * 16
    return max(wanted, FLOOR_MIB.get(service, DEFAULT_FLOOR_MIB))


async def call(client: Any, tool: str, arguments: dict[str, Any]) -> None:
    with contextlib.suppress(MCPError):  # a refusal is an answer; the point is the work done
        await client.call_tool(tool, arguments)


async def reads(token: str, rounds: int) -> None:
    async with connect(URL, token) as client:
        for _ in range(rounds):
            await call(client, "crm__search_accounts", {"query": "Supply", "limit": 20})
            for n in range(1, 11):
                await call(client, "crm__get_account", {"account_id": f"ACC-{n:05d}"})
            await call(client, "crm__list_deals", {"limit": 20})
            await call(client, "tickets__list_tickets", {"limit": 20})
            for n in range(1, 6):
                await call(client, "tickets__get_ticket", {"ticket_id": f"TKT-{n:06d}"})
            await call(client, "handbook__search", {"query": "returns restocking fee"})
            for doc in ("DOC-002", "DOC-003"):
                await call(client, "handbook__get_document", {"document_id": doc})


async def oversize(token: str) -> None:
    async with connect(URL, token) as client:
        for _ in range(5):
            await call(
                client,
                "tickets__create_ticket",
                {"subject": "x" * 10, "description": "y" * 60_000, "account_id": "ACC-00001"},
            )


async def write(token: str, n: int) -> None:
    async with connect(URL, token) as client:
        await call(
            client,
            "tickets__create_ticket",
            {
                "subject": f"RT-mem {n}",
                "description": f"memory probe {n}",
                "account_id": "ACC-00001",
            },
        )


async def volume(token: str, seconds: float) -> int:
    done = 0
    end = time.monotonic() + seconds

    async def worker() -> None:
        nonlocal done
        async with connect(URL, token) as client:
            while time.monotonic() < end:
                await call(client, "handbook__search", {"query": "vacation days"})
                await call(client, "crm__list_deals", {"limit": 5})
                done += 2

    async with anyio.create_task_group() as tasks:
        for _ in range(20):
            tasks.start_soon(worker)
    return done


async def run(support: str, ops: str, approver_id: str | None) -> None:
    async def phase(name: str, work: Any) -> None:
        started = time.monotonic()
        await work()
        print(f"  {name}: {time.monotonic() - started:.0f} s")

    async def phase_reads() -> None:
        async with anyio.create_task_group() as tasks:
            for n in range(40):
                tasks.start_soon(reads, support if n % 2 else ops, 3)

    async def phase_oversize() -> None:
        async with anyio.create_task_group() as tasks:
            for n in range(30):
                tasks.start_soon(oversize, support if n % 2 else ops)

    async def phase_writes() -> None:
        from auto_approver import auto_approving

        async with anyio.create_task_group() as tasks:
            for n in range(30):
                tasks.start_soon(write, ops, n)
            await anyio.sleep(5)  # held: nobody has approved yet
            async with auto_approving(approver_id):
                await anyio.sleep(8)
            tasks.cancel_scope.cancel()

    async def phase_volume() -> None:
        total = await volume(ops, 60)
        print(f"    {total} calls")

    await phase("reads", phase_reads)
    await phase("oversize", phase_oversize)
    await phase("approvals", phase_writes)
    await phase("volume", phase_volume)


def main() -> None:
    if os.environ.get("COMPOSE_PROJECT_NAME") != PROJECT:
        sys.exit(f"measure: run it against the verification stack: COMPOSE_PROJECT_NAME={PROJECT}")
    support, ops = os.environ.get("SUPPORT_TOKEN", ""), os.environ.get("OPS_TOKEN", "")
    if not (support and ops):
        sys.exit(
            "measure: set SUPPORT_TOKEN and OPS_TOKEN (scripts/measure_memory_at_maxima.sh does)"
        )
    print("recreating the long-running services, so each peak starts cold")
    docker("compose", "up", "-d", "--force-recreate", "--wait", *LONG_RUNNING)
    time.sleep(10)
    print("idle (20 s)")
    time.sleep(20)
    print("loads")
    anyio.run(run, support, ops, os.environ.get("APPROVER_ID"))
    time.sleep(5)
    print("\nservice            peak MiB   1.5x, 16 MiB steps")
    for service in LONG_RUNNING:
        peak = peak_mib(service)
        print(f"{service:<18} {peak:>8.1f}   {limit_for(service, peak):>5} MiB")


if __name__ == "__main__":
    main()
