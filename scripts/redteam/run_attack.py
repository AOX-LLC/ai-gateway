#!/usr/bin/env python3
"""Run an attack file against the running stack and judge it. TEST TOOLING for the fictional demo.

    GATEWAY_TOKEN=... POLICY_APPROVER_DATABASE_URL=... \\
        uv run scripts/redteam/run_attack.py scripts/redteam/attacks/export-every-customer.toml \\
        --mode enforce --approve-as <approver id>

With --approve-as the demo approver is played by scripts/auto_approver.py, which needs the caller to
have set LAB_AUTO_APPROVE to yes in the environment (nothing here sets it).

`--mode` says how the gateway is configured, and so what to expect: `enforce` (every layer
enforcing: every call goes as the attack's `enforce` ranges say, with the layer that stopped it
named in telemetry, and the independent oracle finds nothing landed) or `monitor` (the layers that
can be weakened only watch: every call is forwarded, each records the layers that would have
refused it, and the oracle finds the export landed, which shows it can see one).
scripts/run_redteam_check.sh sets the gateway up for each and runs both.

The attacker is a scripted client that does what a planted ticket says (scripted_client.py); the
judge is the oracle (oracle.py), which reads the ticketing and CRM databases as their owner and
shares nothing with the gateway. The recorded side comes from the dashboard's telemetry views, as
its reader role. Needs the gateway's token (GATEWAY_TOKEN), the database owner's
POSTGRES_USER, POSTGRES_PASSWORD and POSTGRES_DB, TELEMETRY_READER_DATABASE_URL, and only ever talks
to 04's own ports (4400-4499). It prints counts, step ids and layer names; never a token, a
password or a customer value. The attack's tickets and the planted one are removed when it ends.
"""

import argparse
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlsplit

import anyio
import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from redteam.attack_format import Attack, load_attack
from redteam.oracle import (
    Evidence,
    Landed,
    attack_succeeded,
    clean_up,
    db_now,
    plant_ticket,
    read_landed,
)
from redteam.prediction import WEAKENABLE, load_effects, predicted_success
from redteam.scripted_client import CallRecord, play

PORT_RANGE = range(4400, 4500)


def _owner_url() -> str:
    user, password, db = (
        os.environ.get(k) for k in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB")
    )
    if not (user and password and db):
        sys.exit("run_attack: set POSTGRES_USER, POSTGRES_PASSWORD and POSTGRES_DB")
    return f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}@127.0.0.1:4402/{db}"


def _check_ports(*urls: str) -> None:
    """Every URL this talks to (the gateway, the databases) is on this machine and in 04's ports: it
    sends a gateway token to one and the database owner's login to another."""
    for url in urls:
        parts = urlsplit(url)
        if parts.hostname not in ("127.0.0.1", "localhost") or parts.port not in PORT_RANGE:
            sys.exit(f"run_attack: {parts.hostname}:{parts.port} is not 127.0.0.1 in 4400-4499")


def _recorded(
    reader_url: str, client: str, since: datetime, expected: int
) -> list[dict[str, object]]:
    """The gateway's own record of this run's calls, oldest first, once all of them are stored."""
    deadline = time.monotonic() + 45
    while True:
        with psycopg.connect(reader_url) as connection:
            rows = connection.execute(
                "SELECT request_id, tool, outcome, blocked_by FROM telemetry.dash_requests"
                " WHERE kind = 'tool_call' AND client_name = %s AND ts >= %s ORDER BY ts",
                (client, since),
            ).fetchall()
            if len(rows) >= expected or time.monotonic() > deadline:
                ids = [row[0] for row in rows]
                would = connection.execute(
                    "SELECT request_id, layer FROM telemetry.dash_layer_verdicts"
                    " WHERE verdict = 'would_block' AND request_id = ANY(%s)",
                    (ids,),
                ).fetchall()
                break
        time.sleep(1)
    layers: dict[object, set[str]] = {}
    for request_id, layer in would:
        layers.setdefault(request_id, set()).add(layer)
    return [
        {"tool": r[1], "outcome": r[2], "blocked_by": r[3], "would_block": layers.get(r[0], set())}
        for r in rows
    ]


def judge(
    attack: Attack,
    mode: str,
    records: list[CallRecord],
    recorded: list[dict[str, object]],
    landed: Landed,
    evidence: Evidence | None = None,
) -> list[str]:
    """Everything that is not as the attack file says it should be, one line each. Whether the
    attack succeeded is the oracle's verdict at the attack's own threshold, and it is compared with
    what the file predicts for this run: every layer on in `enforce`, the weakenable ones only
    watching in `monitor`."""
    failures: list[str] = []
    calls = attack.calls()
    if len(recorded) != len(calls):
        failures.append(f"the gateway recorded {len(recorded)} calls, the client made {len(calls)}")
        return failures
    for (step, i), record, row in zip(calls, records, recorded, strict=True):
        name = f"{step.id} #{i}"
        if row["tool"] != step.tool:
            failures.append(f"{name}: the record is for {row['tool']}, not {step.tool}")
            continue
        if mode == "enforce":
            want = step.expected_enforce(i)
            blocked = row["outcome"] == "blocked"
            if (want.outcome == "blocked") != blocked or record.outcome == "error":
                failures.append(
                    f"{name}: expected {want.outcome}, the gateway recorded {row['outcome']}"
                )
            elif blocked and (row["blocked_by"] != want.blocked_by or record.outcome != "refused"):
                failures.append(
                    f"{name}: blocked by {row['blocked_by']}, expected {want.blocked_by}"
                )
        else:
            want_layers = step.expected_monitor(i).would_block
            if row["outcome"] != "forwarded" or record.outcome == "refused":
                failures.append(f"{name}: a monitor run must forward every call")
            elif row["would_block"] != want_layers:
                failures.append(
                    f"{name}: would_block {sorted(row['would_block'])},"  # type: ignore[call-overload]
                    f" expected {sorted(want_layers)}"
                )
    writes = [(s, i) for s, i in calls if s.tool == "tickets__create_ticket"]
    weakened = frozenset() if mode == "enforce" else WEAKENABLE
    expected = predicted_success(attack, weakened, load_effects())
    observed = (
        attack_succeeded(attack.expect, evidence or Evidence(landed)) if attack.hostile else False
    )
    if mode == "enforce":
        # What lands is what the file lets through: its allowed writes, and an export only where
        # the file predicts one (a gap it records).
        allowed = sum(step.expected_enforce(i).outcome == "allowed" for step, i in writes)
        if len(landed.subjects) != allowed or (not expected and (observed or landed.exported)):
            failures.append(
                f"the oracle found {len(landed.subjects)} attack tickets,"
                f" {landed.customer_values} customer values and {landed.canaries} canaries landed;"
                f" expected {allowed} tickets and no export"
            )
        elif observed != expected:
            failures.append(
                f"the oracle judged the attack {'a success' if observed else 'a failure'}"
                f" at its threshold of {attack.expect.threshold}; the file predicts"
                f" {'a success' if expected else 'a failure'}"
            )
    else:
        if not attack.hostile and landed.exported:
            failures.append("the oracle saw an export in an honest run")
        elif attack.hostile and expected and not observed:
            failures.append(
                "the oracle saw no export in a monitor run: it cannot be trusted to see one"
            )
        elif observed != expected:
            failures.append(
                f"the oracle judged the attack {'a success' if observed else 'a failure'}"
                f" in a monitor run; the file predicts {'a success' if expected else 'a failure'}"
            )
        if len(landed.subjects) != len(writes):
            failures.append(
                f"{len(landed.subjects)} of {len(writes)} writes landed in a monitor run"
            )
    return failures


async def _run(args: argparse.Namespace) -> int:
    attack = load_attack(args.attack)
    token = os.environ.get("GATEWAY_TOKEN", "")
    reader = os.environ.get("TELEMETRY_READER_DATABASE_URL", "")
    if not token or not reader:
        sys.exit("run_attack: set GATEWAY_TOKEN and TELEMETRY_READER_DATABASE_URL")
    owner = _owner_url()
    _check_ports(
        owner, reader, args.url, *filter(None, [os.environ.get("POLICY_APPROVER_DATABASE_URL")])
    )
    planted, since = (
        plant_ticket(owner, attack.plant.account_id, attack.plant.subject, attack.plant.text)
        if attack.plant
        else ("", db_now(owner))
    )
    try:
        if args.approve_as:
            from auto_approver import auto_approving

            async with auto_approving(args.approve_as):
                records = await play(attack, args.url, token, planted)
        else:
            records = await play(attack, args.url, token, planted)
        recorded = await anyio.to_thread.run_sync(
            _recorded, reader, attack.client, since, len(attack.calls())
        )
        landed = read_landed(owner, since, planted, attack.marker)
    finally:
        if not args.keep:
            removed = clean_up(owner, since, planted, attack.marker)
            print(f"removed {removed} tickets made by the run")
    failures = judge(attack, args.mode, records, recorded, landed)
    print(f"attack {attack.id} as {attack.client}, gateway in {args.mode} mode")
    for step in attack.steps:
        mine = [r for r in records if r.step == step.id]
        tally = {o: sum(r.outcome == o for r in mine) for o in ("answered", "refused", "error")}
        print(f"  {step.id}: {len(mine)} calls, {tally}")
    print(
        f"oracle: {len(landed.subjects)} attack tickets landed, {landed.customer_values} customer"
        f" values, {landed.canaries} canaries; exported: {landed.exported}"
    )
    for failure in failures:
        print(f"FAIL  {failure}")
    if failures:
        return 1
    print(f"PASS  {attack.id} ({args.mode})")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("attack", type=Path)
    parser.add_argument("--mode", choices=("enforce", "monitor"), required=True)
    parser.add_argument("--url", default="http://127.0.0.1:4401/mcp")
    parser.add_argument("--approve-as", metavar="APPROVER")
    parser.add_argument("--keep", action="store_true", help="leave the tickets in place")
    args = parser.parse_args()
    if re.search(r"[^\x20-\x7e]", str(args.attack)):
        sys.exit("run_attack: odd path")
    sys.exit(anyio.run(_run, args))


if __name__ == "__main__":
    main()
