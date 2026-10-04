#!/usr/bin/env python3
"""Seed a demo stack so the dashboard has a week of believable, fictional traffic to show.

    uv run scripts/seed_dashboard_demo.py --tokens-file demo.json     # needs the stack up

Two parts, both safe to read before running:

1. A backfill of about seven days of telemetry (requests, layer verdicts, failed logins, the
   pipeline configuration), written through the telemetry writer role, the way the gateway writes
   it. It has a daily rhythm (quiet nights, busy afternoons, two quiet days) and four episodes: a
   client probing tools it was not given, a spray of invalid tokens, a burst that a layer in
   monitor mode would have limited, and a slow upstream. Everything is made up (Harborline Supply
   Co. is fictional). The same seed and anchor give the same rows: `build_backfill` is pure and
   tested. 2. Approvals, through the real path: calls go through the running gateway as the two
   demo bots, wait for a person and come back "pending"; two demo approvers decide one each with
   the approver tool's own code, under their own database logins; one more request is made with a
   two-second lifetime and left to expire. The result is 4 pending, 1 approved, 1 rejected and 1
   expired. No approval is written by hand.

The demo stack must start empty (this refuses a stack that already has requests) and should run
with compose.demo.yaml (a one-day approval lifetime, a three-second hold, and rate limits in
monitor mode: see that file). The passwords come from .env and the approvers' logins from the
environment; nothing secret is printed."""

import argparse
import hashlib
import json
import math
import os
import random
import re
import subprocess
import sys
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

DEFAULT_SEED = 20261003
DAYS = 7
HOLD_MS = 3000.0
"""How long the demo gateway holds a write for approval (compose.demo.yaml sets 3 s)."""

LAYERS = [
    ("scope", "enforce"),
    ("allowlist", "enforce"),
    ("rate_limit", "monitor"),
    ("schema", "enforce"),
    ("pinned_descriptions", "enforce"),
    ("egress", "enforce"),
    ("canary", "enforce"),
    ("classifier", "enforce"),
    ("approval", "enforce"),
]
"""The demo pipeline: rate limiting in monitor mode, so the dashboard has "would block" to show."""

SUPPORT, OPS, DECOY = "harborline-support-bot", "harborline-ops-bot", "harborline-decoy-bot"
SCOPES = {
    SUPPORT: [
        "handbook__search",
        "handbook__get_document",
        "crm__search_accounts",
        "crm__get_account",
        "crm__list_deals",
        "tickets__get_ticket",
        "tickets__list_tickets",
        "tickets__create_ticket",
        "tickets__add_comment",
    ],
    OPS: [],  # filled below: the support scopes and two more
    DECOY: [],
}
SCOPES[OPS] = [*SCOPES[SUPPORT], "tickets__change_status", "tickets__assign"]
WRITES = {
    "tickets__create_ticket",
    "tickets__add_comment",
    "tickets__change_status",
    "tickets__assign",
}
POPULARITY = {
    "tickets__list_tickets": 18,
    "tickets__get_ticket": 18,
    "crm__search_accounts": 10,
    "crm__get_account": 9,
    "crm__list_deals": 5,
    "handbook__search": 14,
    "handbook__get_document": 6,
    "tickets__create_ticket": 5,
    "tickets__add_comment": 6,
    "tickets__change_status": 4,
    "tickets__assign": 4,
}
MEDIAN_MS = {
    "tickets__list_tickets": 14.0,
    "tickets__get_ticket": 7.0,
    "crm__search_accounts": 9.0,
    "crm__get_account": 6.0,
    "crm__list_deals": 11.0,
    "handbook__search": 9.0,
    "handbook__get_document": 6.0,
    "tickets__create_ticket": 22.0,
    "tickets__add_comment": 18.0,
    "tickets__change_status": 20.0,
    "tickets__assign": 17.0,
}
CLIENT_WEIGHTS = [(SUPPORT, 0.50), (OPS, 0.38), (DECOY, 0.12)]
AUTH_REASONS = [
    ("unknown_token", 0.30),
    ("wrong_secret", 0.30),
    ("expired", 0.30),
    ("revoked", 0.10),
]
_B32 = "abcdefghijklmnopqrstuvwxyz234567"


@dataclass
class Backfill:
    """Everything the backfill would write, as plain rows, so it can be compared and tested."""

    config_sha256: str
    config_layers: list[dict[str, str]]
    first_seen: datetime
    requests: list[dict[str, Any]] = field(default_factory=list)
    verdicts: list[dict[str, Any]] = field(default_factory=list)
    auth_failures: list[dict[str, Any]] = field(default_factory=list)
    usage: list[dict[str, Any]] = field(default_factory=list)


def config_fingerprint(layers: list[tuple[str, str]]) -> str:
    """The gateway's own fingerprint of these layer modes (a test pins it to the gateway's)."""
    source = {
        "layers": dict(layers),
        "allow_floor_override": False,
        "allow_unaudited_writes": False,
    }
    return hashlib.sha256(json.dumps(source, sort_keys=True).encode()).hexdigest()


def _poisson(rng: random.Random, lam: float) -> int:
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


QUIET_DAYS_BACK = (
    3,
    4,
)  # the two quiet days, counted back from the anchor's day: a made-up weekend


def default_anchor(now: datetime) -> datetime:
    """The newest 21:57 UTC at or before `now`.

    Always the same time of day, and the quiet days are counted from it and not from the calendar,
    so the same seed gives the same counts and shapes whatever day and hour the seed is run."""
    anchor = now.astimezone(UTC).replace(hour=21, minute=57, second=0, microsecond=0)
    return anchor if anchor <= now else anchor - timedelta(days=1)


def profile(hour: float, quiet: bool) -> float:
    """Load over a day: quiet nights, a morning ramp, an afternoon peak; quiet days a third."""
    day = max(0.0, math.sin(math.pi * (hour - 6.0) / 14.0)) ** 1.5 if 6.0 <= hour <= 20.0 else 0.0
    return (0.12 + 0.88 * day) * (0.35 if quiet else 1.0)


def _choose(rng: random.Random, weighted: list[tuple[str, float]]) -> str:
    point, total = rng.random() * sum(weight for _, weight in weighted), 0.0
    for name, weight in weighted:
        total += weight
        if point <= total:
            return name
    return weighted[-1][0]


def _uuid(rng: random.Random) -> str:
    return str(uuid.UUID(int=rng.getrandbits(128), version=4))


def _hex(rng: random.Random, bits: int) -> str:
    return f"{rng.getrandbits(bits):0{bits // 4}x}"


@dataclass(frozen=True)
class Episode:
    name: str
    start: datetime
    minutes: int


def episodes(anchor: datetime) -> dict[str, Episode]:
    """Four episodes at fixed times of day on fixed days back from the anchor."""
    midnight = anchor.replace(hour=0, minute=0, second=0, microsecond=0)

    def at(days_back: int, hour: int, minute: int, length: int, name: str) -> Episode:
        return Episode(
            name,
            midnight - timedelta(days=days_back) + timedelta(hours=hour, minutes=minute),
            length,
        )

    return {
        "probing": at(5, 14, 5, 25, "a client probing tools it was not given"),
        "spray": at(3, 3, 10, 40, "a spray of invalid tokens"),
        "slow": at(2, 16, 0, 120, "a slow upstream"),
        "burst": at(1, 10, 20, 20, "a burst a monitored layer would have limited"),
    }


def _within(moment: datetime, episode: Episode) -> bool:
    return episode.start <= moment < episode.start + timedelta(minutes=episode.minutes)


def build_backfill(
    seed: int, anchor: datetime, days: int = DAYS, hold_ms: float = HOLD_MS
) -> Backfill:
    """About `days` of telemetry ending at `anchor`; the same seed and anchor give the same rows."""
    rng = random.Random(seed)  # noqa: S311 - fictional demo data, not security
    anchor = anchor.astimezone(UTC).replace(second=0, microsecond=0)
    sha = config_fingerprint(LAYERS)
    result = Backfill(
        sha, [{"name": n, "mode": m} for n, m in LAYERS], anchor - timedelta(days=days)
    )
    eps = episodes(anchor)
    client_ids = {
        name: str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{name}.harborline.example")) for name in SCOPES
    }
    start = anchor - timedelta(days=days)
    for minute in range(days * 1440):
        moment = start + timedelta(minutes=minute)
        quiet = (anchor.date() - moment.date()).days in QUIET_DAYS_BACK
        rate = 1.6 * profile(moment.hour + moment.minute / 60, quiet)
        calls = _poisson(rng, rate)
        extra: list[str] = []
        if _within(moment, eps["probing"]):
            extra += [DECOY] * 12
        if _within(moment, eps["burst"]):
            extra += [SUPPORT] * 20
        for index in range(calls + len(extra)):
            forced = extra[index - calls] if index >= calls else None
            second = rng.random() * 59.9
            _call(result, rng, moment + timedelta(seconds=second), forced, client_ids, eps, hold_ms)
        if calls and rng.random() < 0.04:
            _tools_list(
                result, rng, moment + timedelta(seconds=rng.random() * 59.9), client_ids, sha
            )
        _auth(result, rng, moment, eps)
    result.requests.sort(key=lambda r: (r["ts"], r["request_id"]))
    result.verdicts.sort(key=lambda v: (v["ts"], v["request_id"], v["ordinal"]))
    result.auth_failures.sort(key=lambda a: (a["ts"], a["event_id"]))
    result.usage.sort(key=lambda u: (u["ts"], u["usage_id"]))
    return result


def _auth(result: Backfill, rng: random.Random, moment: datetime, eps: dict[str, Episode]) -> None:
    rate = 0.007  # about one a few hours
    if _within(moment, eps["spray"]):
        rate = 8.0
    elif _within(moment, eps["probing"]):
        rate = 1.5
    for _ in range(_poisson(rng, rate)):
        sprayed = _within(moment, eps["spray"])
        reason = _choose(
            rng,
            [
                ("unknown_token", 0.55),
                ("wrong_secret", 0.25),
                ("throttled", 0.15),
                ("revoked", 0.05),
            ]
            if sprayed
            else AUTH_REASONS,
        )
        known = reason in {"wrong_secret", "expired", "revoked"}
        result.auth_failures.append(
            {
                "event_id": _uuid(rng),
                "ts": moment + timedelta(seconds=rng.random() * 59.9),
                "reason": reason,
                "lookup_id": "".join(rng.choice(_B32) for _ in range(8))
                if known or reason == "unknown_token"
                else None,
            }
        )


def _tools_list(
    result: Backfill, rng: random.Random, ts: datetime, client_ids: dict[str, str], sha: str
) -> None:
    client = _choose(rng, [(SUPPORT, 0.5), (OPS, 0.5)])
    result.requests.append(
        _request(
            rng,
            ts,
            kind="tools_list",
            client=client,
            client_ids=client_ids,
            tool=None,
            effect=None,
            outcome="listed",
            blocked_by=None,
            deny_code=None,
            upstream=None,
            duration=rng.uniform(0.3, 1.2),
            sha=sha,
        )
        | {"tools_available": 11, "tools_returned": len(SCOPES[client])}
    )


def _request(
    rng: random.Random,
    ts: datetime,
    *,
    kind: str,
    client: str,
    client_ids: dict[str, str],
    tool: str | None,
    effect: str | None,
    outcome: str,
    blocked_by: str | None,
    deny_code: str | None,
    upstream: str | None,
    duration: float,
    sha: str,
) -> dict[str, Any]:
    return {
        "request_id": _uuid(rng),
        "ts": ts,
        "kind": kind,
        "client_id": client_ids[client],
        "client_name": client,
        "tool": tool,
        "namespace": tool.split("__", 1)[0] if tool else None,
        "effect": effect,
        "effect_source": "policy" if effect else None,
        "outcome": outcome,
        "blocked_by": blocked_by,
        "deny_code": deny_code,
        "upstream_status": upstream,
        "duration_ms": round(duration, 3),
        "upstream_duration_ms": round(duration * 0.93, 3) if upstream else None,
        "args_sha256": _hex(rng, 256) if tool else None,
        "protocol_version": "2025-11-25",
        "pipeline_config_sha256": sha,
        "trace_id": None,
        "tools_available": None,
        "tools_returned": None,
    }


def _call(
    result: Backfill,
    rng: random.Random,
    ts: datetime,
    forced: str | None,
    client_ids: dict[str, str],
    eps: dict[str, Episode],
    hold_ms: float,
) -> None:
    client = forced or _choose(rng, CLIENT_WEIGHTS)
    probing = forced == DECOY
    pool = list(POPULARITY) if client == DECOY or probing else SCOPES[client]
    tool = _choose(rng, [(name, float(POPULARITY[name])) for name in pool])
    effect = "write" if tool in WRITES else "read"
    sha = result.config_sha256
    slow = _within(ts, eps["slow"]) and tool.startswith("tickets__")
    burst = forced == SUPPORT and _within(ts, eps["burst"])

    verdicts: list[tuple[str, str, str | None]] = []  # layer, verdict, code
    blocked_by = deny_code = None
    upstream: str | None = "ok"
    duration = rng.lognormvariate(math.log(MEDIAN_MS[tool]), 0.45) * (
        6.0 if rng.random() < 0.015 else 1.0
    )

    if tool not in SCOPES[client]:
        blocked_by, deny_code = "scope", "tool_unavailable"
        verdicts = [("scope", "deny", deny_code)]
        duration, upstream = rng.uniform(0.1, 0.4), None
    else:
        verdicts.append(("scope", "allow", None))
        if client == SUPPORT and tool == "tickets__create_ticket" and rng.random() < 0.05:
            blocked_by, deny_code = "allowlist", "allowlist_violation"
            verdicts.append(("allowlist", "deny", deny_code))
            duration, upstream = rng.uniform(0.2, 0.6), None
        else:
            verdicts.append(("allowlist", "allow", None))
            verdicts.append(
                (
                    "rate_limit",
                    "would_block" if burst and rng.random() < 0.6 else "allow",
                    "rate_limited" if burst else None,
                )
            )
            # The injection layers pass the demo's honest traffic (it has no attack for them yet).
            # About one call in fifty has a text the recordings do not hold: the classifier says
            # `unclassified` (never clean). Model usage has its own stream, seeded from the plan's.
            usage_rng = random.Random(rng.getrandbits(48))  # noqa: S311
            unrecorded = usage_rng.random() < 0.02
            verdicts.extend(
                (layer, "allow", None)
                for layer in ("schema", "pinned_descriptions", "egress", "canary")
            )
            verdicts.append(
                ("classifier", "unclassified", "classifier_unrecorded")
                if unrecorded
                else ("classifier", "allow", None)
            )
            if effect == "write" and rng.random() < 0.45:
                blocked_by, deny_code = "approval", "approval_pending"
                verdicts.append(("approval", "deny", deny_code))
                duration, upstream = hold_ms + rng.uniform(0, 250), None
            else:
                verdicts.append(("approval", "allow", None))
    if upstream == "ok":
        if slow:
            duration *= 25.0
        roll = rng.random()
        if roll < (0.06 if slow else 0.012):
            upstream = "error"
        elif roll > 0.998:
            upstream, duration = "timeout", rng.uniform(7000, 9000)
    outcome = "blocked" if blocked_by else "forwarded"
    row = _request(
        rng,
        ts,
        kind="tool_call",
        client=client,
        client_ids=client_ids,
        tool=tool,
        effect=effect,
        outcome=outcome,
        blocked_by=blocked_by,
        deny_code=deny_code,
        upstream=upstream,
        duration=duration,
        sha=sha,
    )
    result.requests.append(row)
    if any(layer == "classifier" for layer, _, _ in verdicts):
        _usage(result, usage_rng, ts, row["request_id"], effect, unrecorded)
    modes = dict(LAYERS)
    for ordinal, (layer, verdict, code) in enumerate(verdicts):
        result.verdicts.append(
            {
                "request_id": row["request_id"],
                "ordinal": ordinal,
                "ts": ts,
                "layer": layer,
                "hook": "before_call",
                "mode": modes[layer],
                "verdict": verdict,
                "code": code if verdict != "allow" else None,
                "tools_removed": None,
                "duration_ms": round(rng.uniform(0.01, 0.09), 3),
            }
        )


PRICE_PER_MTOK = (1.0, 5.0)
"""Dollars per million input and output tokens of the small tier (as in config/agent-core.toml)."""


def _usage(
    result: Backfill,
    rng: random.Random,
    ts: datetime,
    request_id: str,
    effect: str,
    unrecorded: bool,
) -> None:
    """The classifier's model calls for one request: one to three units, answered from recordings
    (mode `replay`, so the cost is the recording's and nothing was billed)."""
    for unit in range(1 + int(rng.random() * (2 if effect == "write" else 3))):
        missing = unrecorded and unit == 0
        tokens_in = 0 if missing else rng.randint(180, 900)
        tokens_out = 0 if missing else rng.randint(18, 24)
        result.usage.append(
            {
                "usage_id": str(uuid.UUID(int=rng.getrandbits(128), version=4)),
                "request_id": request_id,
                "ts": ts,
                "layer": "classifier",
                "purpose": "tool_arguments" if effect == "write" else "tool_result",
                "model": "claude-haiku-4-5-20251001",
                "tier": "small",
                "mode": "replay",
                "input_tokens": tokens_in,
                "output_tokens": tokens_out,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "cost_usd": round(
                    (tokens_in * PRICE_PER_MTOK[0] + tokens_out * PRICE_PER_MTOK[1]) / 1e6, 8
                ),
                "latency_ms": round(rng.uniform(1.0, 6.0), 3),
                "status": "unrecorded" if missing else "ok",
            }
        )


def summary(backfill: Backfill) -> dict[str, Any]:
    calls = [r for r in backfill.requests if r["kind"] == "tool_call"]
    return {
        "requests": len(backfill.requests),
        "tool_calls": len(calls),
        "blocked": sum(r["outcome"] == "blocked" for r in calls),
        "by_layer": dict(
            sorted(Counter(r["blocked_by"] for r in calls if r["blocked_by"]).items())
        ),
        "would_block": sum(v["verdict"] == "would_block" for v in backfill.verdicts),
        "auth_failures": len(backfill.auth_failures),
        "auth_reasons": dict(sorted(Counter(a["reason"] for a in backfill.auth_failures).items())),
        "upstream_errors": sum(r["upstream_status"] in {"error", "timeout"} for r in calls),
        "model_calls": len(backfill.usage),
        "unrecorded": sum(u["status"] == "unrecorded" for u in backfill.usage),
    }


# --- writing ---------------------------------------------------------------------------------

REQUEST_COLUMNS = [
    "request_id",
    "ts",
    "kind",
    "client_id",
    "client_name",
    "tool",
    "namespace",
    "effect",
    "effect_source",
    "outcome",
    "blocked_by",
    "deny_code",
    "upstream_status",
    "duration_ms",
    "upstream_duration_ms",
    "args_sha256",
    "protocol_version",
    "pipeline_config_sha256",
    "trace_id",
    "tools_available",
    "tools_returned",
]
VERDICT_COLUMNS = [
    "request_id",
    "ordinal",
    "ts",
    "layer",
    "hook",
    "mode",
    "verdict",
    "code",
    "tools_removed",
    "duration_ms",
]
AUTH_COLUMNS = ["event_id", "ts", "reason", "lookup_id"]
USAGE_COLUMNS = [
    "usage_id",
    "request_id",
    "ts",
    "layer",
    "purpose",
    "model",
    "tier",
    "mode",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "cost_usd",
    "latency_ms",
    "status",
]


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        match = re.match(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if match:
            values[match.group(1)] = match.group(2).strip().strip("'\"")
    return values


def database_url(env: dict[str, str], role: str, password_key: str, host: str, port: int) -> str:
    password = env.get(password_key)
    if not password:
        sys.exit(f"seed_dashboard_demo: {password_key} is not set in the env file")
    name = env.get("POSTGRES_DB", "ai_gateway")
    return f"postgresql://{role}:{quote(password, safe='')}@{host}:{port}/{name}"


def write_backfill(url: str, backfill: Backfill) -> None:
    import psycopg
    from psycopg.types.json import Jsonb

    def insert(cursor: Any, table: str, columns: list[str], rows: list[dict[str, Any]]) -> None:
        names, marks = ", ".join(columns), ", ".join(["%s"] * len(columns))
        sql = f"INSERT INTO telemetry.{table} ({names}) VALUES ({marks}) ON CONFLICT DO NOTHING"  # noqa: S608
        cursor.executemany(sql, [[row[c] for c in columns] for row in rows])

    with psycopg.connect(url) as connection, connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO telemetry.pipeline_configs (sha256, first_seen, layers)"
            " VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
            (backfill.config_sha256, backfill.first_seen, Jsonb(backfill.config_layers)),
        )
        for start in range(0, len(backfill.requests), 1000):
            insert(cursor, "requests", REQUEST_COLUMNS, backfill.requests[start : start + 1000])
        for start in range(0, len(backfill.verdicts), 2000):
            insert(
                cursor, "layer_verdicts", VERDICT_COLUMNS, backfill.verdicts[start : start + 2000]
            )
        insert(cursor, "auth_failures", AUTH_COLUMNS, backfill.auth_failures)
        for start in range(0, len(backfill.usage), 2000):
            insert(cursor, "model_usage", USAGE_COLUMNS, backfill.usage[start : start + 2000])


DEMO_PROJECT = "ai-gateway-demo"


def not_the_demo_stack(port: int) -> str | None:
    """Why the database published on this port is not the demo stack's, or None when it is.

    A fresh real stack has no requests either, so "empty" does not say whose it is: the container
    that publishes the port must belong to the demo Compose project."""
    try:
        done = subprocess.run(  # noqa: S603 - fixed command, port from this script's own argument
            [  # noqa: S607 - docker on PATH, as everywhere else here
                "docker",
                "ps",
                "--filter",
                f"publish={port}",
                "--format",
                '{{.Label "com.docker.compose.project"}}',
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        return f"cannot tell which stack owns port {port} ({error.__class__.__name__})"
    projects = {line for line in done.stdout.split() if line}
    if projects == {DEMO_PROJECT}:
        return None
    return (
        f"the database on port {port} is not the demo stack's (Compose project"
        f" {sorted(projects) or 'none found'}, not {DEMO_PROJECT});"
        " run scripts/run_dashboard_demo.sh"
    )


def existing_requests(reader_url: str) -> int:
    import psycopg

    with psycopg.connect(reader_url) as connection:
        row = connection.execute("SELECT count(*) FROM telemetry.dash_requests").fetchone()
    return int(row[0]) if row else 0


def approval_counts(reader_url: str) -> dict[str, int]:
    import psycopg

    sql = (
        "SELECT CASE WHEN status = 'pending' AND expires_at <= now()"
        " THEN 'expired' ELSE status END, count(*)"
        " FROM policy.dash_approvals GROUP BY 1"
    )
    with psycopg.connect(reader_url) as connection:
        return {str(status): int(count) for status, count in connection.execute(sql).fetchall()}


# --- approvals through the real path -----------------------------------------------------------

PENDING_CALLS = [
    (
        OPS,
        "tickets__create_ticket",
        {
            "subject": "Dock 4 scanner will not pair",
            "description": "Fictional demo ticket.",
            "priority": "normal",
            "account_id": "ACC-00007",
        },
    ),
    (OPS, "tickets__assign", {"ticket_id": "TKT-000011", "assignee": "dana.kerr"}),
    (OPS, "tickets__change_status", {"ticket_id": "TKT-000012", "status": "closed"}),
    (
        SUPPORT,
        "tickets__add_comment",
        {
            "ticket_id": "TKT-000013",
            "body": "Replacement label printer ships Tuesday (fictional demo note).",
        },
    ),
    (
        SUPPORT,
        "tickets__create_ticket",
        {
            "subject": "Pallet wrap running short",
            "description": "Fictional demo ticket.",
            "priority": "low",
            "account_id": "ACC-00012",
        },
    ),
    (
        SUPPORT,
        "tickets__add_comment",
        {
            "ticket_id": "TKT-000014",
            "body": "Customer confirmed the new delivery window (fictional demo note).",
        },
    ),
]
"""Six writes through the gateway wait for a person and come back pending. One (the assign) is
approved and one (the status change) rejected; four stay pending."""


async def seed_approvals(
    env: dict[str, str], args: argparse.Namespace, tokens: dict[str, str]
) -> None:
    import anyio
    import httpx2
    import psycopg
    from aox_agent_core.approvals import Decision
    from mcp.client import Client
    from mcp.client.streamable_http import streamable_http_client
    from pydantic import SecretStr

    from ai_gateway.approver.cli import Approvals
    from ai_gateway.hashing import configure_hash_key
    from ai_gateway.pipeline.types import CallContext, ClientIdentity, ToolCall
    from ai_gateway.policy import approval_queue_on, policy_url
    from ai_gateway.policy.approvals import PostgresApprovalGate
    from ai_gateway.policy.database import BoundedPostgresDatabase
    from ai_gateway.policy.roles import load_roles_by_action

    # The calls below are built here with the gateway's code, which hashes their arguments under the
    # gateway's key: the same one, from the same .env.
    configure_hash_key(env["GATEWAY_ARGUMENT_HASH_KEY"].encode())
    roles = load_roles_by_action(Path(args.roles_file))
    app_url = database_url(env, "gateway_app", "GATEWAY_APP_DB_PASSWORD", args.host, args.port)
    with psycopg.connect(app_url) as connection:
        ids = {
            str(name): uuid.UUID(str(client_id))
            for client_id, name in connection.execute("SELECT id, name FROM clients").fetchall()
        }

    # 1. One request with a two-second lifetime, made with the gateway's own gate, left to expire.
    gate_url = database_url(
        env, "policy_gateway", "POLICY_GATEWAY_DB_PASSWORD", args.host, args.port
    )
    gate = PostgresApprovalGate(
        approval_queue_on(BoundedPostgresDatabase(SecretStr(policy_url(gate_url)), concurrency=2)),
        ttl_s=2,
        hold_s=0,
        roles_by_action=roles,
    )
    expiring = ToolCall.create(
        "tickets__assign",
        "tickets",
        "assign",
        {"ticket_id": "TKT-000015", "assignee": "priya.nair"},
        "write",
        upstream_identity="",
    )
    await gate.decide(
        CallContext(
            request_id=uuid.uuid4(),
            client=ClientIdentity(id=ids[OPS], name=OPS, scopes=frozenset()),
            session_id=None,
            protocol_version="2025-11-25",
        ),
        expiring,
    )
    await anyio.sleep(3.5)

    # 2. Six writes through the running gateway; each waits out the hold and comes back pending.
    async def call(client: str, tool: str, arguments: dict[str, Any]) -> None:
        http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {tokens[client]}"}, timeout=60)
        async with (
            http,
            Client(
                streamable_http_client(args.gateway_url, http_client=http), mode="legacy"
            ) as session,
        ):
            await session.call_tool(tool, arguments)

    async with anyio.create_task_group() as tasks:
        for client, tool, arguments in PENDING_CALLS:
            tasks.start_soon(call, client, tool, arguments)

    # 3. Two approvers decide one each, as themselves, with the approver tool's own code.
    first = Approvals(os.environ["DEMO_APPROVER_1_URL"], roles)
    second = Approvals(os.environ["DEMO_APPROVER_2_URL"], roles)
    pending = {
        (r.action, str(r.requested_by)): r
        for r in await first.queue.list_pending(await first.principal())
    }
    approve = next(
        r
        for (action, _), r in pending.items()
        if action == "tickets__assign" and ids[OPS].hex in r.requested_by.replace("-", "")
    )
    reject = next(r for (action, _), r in pending.items() if action == "tickets__change_status")
    await first.decide(approve.id, Decision.APPROVE, None)
    await second.decide(
        reject.id,
        Decision.REJECT,
        "Not today: confirm the ticket is resolved first (fictional demo reason).",
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument(
        "--tokens-file", type=Path, help="seed-demo's JSON of client tokens (for the approvals)"
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--days", type=int, default=DAYS)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4402, help="Postgres, published on the host")
    parser.add_argument("--gateway-url", default="http://127.0.0.1:4401/mcp")
    parser.add_argument("--roles-file", default="config/approval_roles.toml")
    parser.add_argument("--hold-ms", type=float, default=HOLD_MS)
    parser.add_argument(
        "--dry-run", action="store_true", help="print what the backfill would hold and stop"
    )
    parser.add_argument("--skip-approvals", action="store_true")
    parser.add_argument(
        "--allow-existing", action="store_true", help="seed a stack that already has requests"
    )
    args = parser.parse_args(argv)

    anchor = default_anchor(datetime.now(UTC))
    backfill = build_backfill(args.seed, anchor, args.days, args.hold_ms)
    print(
        f"backfill (seed {args.seed}, {args.days} days to {anchor:%Y-%m-%d %H:%M} UTC):"
        f" {json.dumps(summary(backfill))}"
    )
    if args.dry_run:
        return
    refusal = not_the_demo_stack(args.port)
    if refusal:
        sys.exit(f"seed_dashboard_demo: {refusal}")
    env = read_env(args.env_file)
    reader = database_url(
        env, "telemetry_reader", "TELEMETRY_READER_DB_PASSWORD", args.host, args.port
    )
    if existing_requests(reader) and not args.allow_existing:
        sys.exit(
            "seed_dashboard_demo: this stack already has requests; the demo state needs an empty"
            " one (--allow-existing to add to it)"
        )
    write_backfill(
        database_url(env, "telemetry_writer", "TELEMETRY_WRITER_DB_PASSWORD", args.host, args.port),
        backfill,
    )
    print("backfill written")
    if args.skip_approvals:
        return
    if not args.tokens_file:
        sys.exit(
            "seed_dashboard_demo: --tokens-file is needed for the approvals (or --skip-approvals)"
        )
    for name in ("DEMO_APPROVER_1_URL", "DEMO_APPROVER_2_URL"):
        if not os.environ.get(name):
            sys.exit(
                f"seed_dashboard_demo: set {name} (the demo approver's login URL;"
                " scripts/run_dashboard_demo.sh does)"
            )
    import anyio

    tokens = json.loads(args.tokens_file.read_text())
    anyio.run(seed_approvals, env, args, tokens)
    counts = approval_counts(reader)
    print(f"approvals: {json.dumps(dict(sorted(counts.items())))}")
    expected = {"pending": 4, "approved": 1, "rejected": 1, "expired": 1}
    if counts != expected:
        sys.exit(f"seed_dashboard_demo: expected {expected}, found {counts}")


if __name__ == "__main__":
    main()
