"""Run one attack as a client of its own and observe what happened: the calls it made, what the
gateway's telemetry says of each, and what the independent oracle finds in the databases.

This does not judge: `scorecard_run.observe` turns a `Ran` into an `Observation`. It talks only to
04's own ports (4400-4499) and prints nothing. Harborline Supply Co. is fictional.
"""

import time
from dataclasses import dataclass
from typing import Any

import anyio
import psycopg

from redteam.attack_format import Attack
from redteam.oracle import (
    Changes,
    Landed,
    clean_up,
    db_now,
    diff_snapshots,
    plant_ticket,
    read_landed,
    take_snapshot,
)
from redteam.scripted_client import AttackStoppedError, CallRecord, play

TELEMETRY_WAIT_S = 60.0


@dataclass
class Ran:
    attack: Attack
    records: list[CallRecord]
    rows: list[dict[str, Any]]
    """One row for each call the gateway recorded, oldest first: `tool`, `outcome`, `blocked_by`,
    `duration_ms` and `verdicts` (layer -> verdict), read back from the gateway's own telemetry."""
    landed: Landed
    changes: Changes | None
    incomplete: bool


def _telemetry(reader_url: str, client: str, since: Any, expected: int) -> list[dict[str, Any]]:
    """The gateway's own record of this client's calls, once all of them are stored."""
    deadline = time.monotonic() + TELEMETRY_WAIT_S
    while True:
        with psycopg.connect(reader_url) as connection:
            rows = connection.execute(
                "SELECT request_id, tool, outcome, blocked_by, duration_ms"
                " FROM telemetry.dash_requests"
                " WHERE kind = 'tool_call' AND client_name = %s AND ts >= %s ORDER BY ts",
                (client, since),
            ).fetchall()
            if len(rows) >= expected or time.monotonic() > deadline:
                ids = [row[0] for row in rows]
                verdicts = connection.execute(
                    "SELECT request_id, layer, verdict FROM telemetry.dash_layer_verdicts"
                    " WHERE request_id = ANY(%s) AND verdict IN ('would_block', 'unclassified')",
                    (ids,),
                ).fetchall()
                break
        time.sleep(0.5)
    by_request: dict[Any, dict[str, str]] = {}
    for request_id, layer, verdict in verdicts:
        by_request.setdefault(request_id, {})[layer] = verdict
    return [
        {
            "tool": r[1],
            "outcome": r[2],
            "blocked_by": r[3],
            "duration_ms": float(r[4]) if r[4] is not None else 0.0,
            "verdicts": by_request.get(r[0], {}),
        }
        for r in rows
    ]


async def run_attack_once(
    attack: Attack,
    *,
    gateway_url: str,
    token: str,
    client_name: str,
    owner_url: str,
    reader_url: str,
) -> Ran:
    """Plant what the attack plants, play it, read back the telemetry and the databases, and clean
    up the tickets it made. An attack the oracle judges by a diff of the ticketing data also takes
    the snapshots (the caller must run it alone and restore the data afterwards)."""
    diffed = attack.expect.oracle == "unauthorized-write"
    before = await anyio.to_thread.run_sync(take_snapshot, owner_url) if diffed else None
    if attack.plant is not None:
        planted, since = await anyio.to_thread.run_sync(
            plant_ticket,
            owner_url,
            attack.plant.account_id,
            attack.plant.subject,
            attack.plant.text,
        )
    else:
        planted, since = "", await anyio.to_thread.run_sync(db_now, owner_url)
    records: list[CallRecord] = []
    incomplete = False
    try:
        try:
            await play(attack, gateway_url, token, planted, partial=records)
        except AttackStoppedError:
            incomplete = True
        rows = await anyio.to_thread.run_sync(
            _telemetry, reader_url, client_name, since, len(records)
        )
        landed = await anyio.to_thread.run_sync(
            read_landed, owner_url, since, planted, attack.marker
        )
        changes = None
        if before is not None:
            after = await anyio.to_thread.run_sync(take_snapshot, owner_url)
            changes = diff_snapshots(before, after)
    finally:
        await anyio.to_thread.run_sync(clean_up, owner_url, since, planted, attack.marker)
    return Ran(attack, records, rows, landed, changes, incomplete)
