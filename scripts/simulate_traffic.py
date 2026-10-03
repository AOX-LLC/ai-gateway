"""Drive a seeded, repeatable mix of traffic through the gateway as both Harborline bots.

    uv run scripts/simulate_traffic.py --tokens-file demo.json --calls 300 --verify
    uv run scripts/simulate_traffic.py --tokens-file demo.json --duration 600   # a slow run

It fills the dashboard for demos and screenshots, and needs no model API key: the clients are
scripted. Harborline Supply Co. is fictional, and so is everything the calls ask for.

The mix is made of normal calls (reads, and a few writes to the fictional ticketing data), calls
the scope layer refuses (a tool the bot was not granted, a tool that does not exist) and
requests that fail authentication (no token, a malformed one, an unknown one, a wrong secret).
The same --seed gives the same sequence of calls, so the counts are the same every run;
latencies are real, and a --duration spreads the calls over that many seconds.

Tokens: `--tokens-file` takes the JSON that `gateway-admin seed-demo` prints (keep it out of
the repository), or set SIM_SUPPORT_TOKEN and SIM_OPS_TOKEN. They are never printed.

--verify reads the dashboard's views as the telemetry_reader role (TELEMETRY_READER_DATABASE_URL)
and compares what the gateway stored with what was sent, exactly. With POLICY_AUDITOR_DATABASE_URL
set it does the same for the audit log: a record for every call, and a record before every write
that ran. It assumes nothing else sent traffic while the simulator ran.
"""

import argparse
import json
import os
import random
import sys
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import anyio
import httpx2
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError

from auto_approver import auto_approving

DEFAULT_SEED = 20261002
SUPPORT, OPS = "harborline-support-bot", "harborline-ops-bot"
DECOY = "harborline-decoy-bot"
"""A client with no scopes whose token the wrong-secret attempts use: the failed-login throttle
locks a token id out, and the bots the simulator is driving must not be the ones it locks."""
TOOL_BURSTS = {"crm__list_deals": 4}
"""Tools with a limit of their own (config/rate_limits.toml): the calls a client may make before the
rest are refused. The refill is too slow to matter in a run (a test pins this to the file)."""
THROTTLE_PER_ID = 5
"""Failed logins for one token id before a 429 (a test pins it to the gateway's default)."""
MIX = {"normal": 0.76, "out_of_scope": 0.12, "auth_failure": 0.08, "urgent_ticket": 0.04}
WRITE_SHARE = 0.10
"""Of the normal calls, this share writes to the fictional ticketing data."""

# Tools the support bot was not granted (the ops bot has them), and tools nobody has.
OPS_ONLY = ["tickets__change_status", "tickets__assign"]
UNKNOWN_TOOLS = ["crm__delete_account", "tickets__export_all", "handbook__upload_document"]
AUTH_CASES = ["missing", "malformed", "unknown_token", "wrong_secret"]
AUTH_WEIGHTS = [1, 1, 1, 3]
"""Wrong secrets are the likeliest, so a run goes past the per-id limit on the decoy's token."""
# Each case is stored under a reason of the same name (AuthFailureReason in the gateway).

_SEARCHES = [
    "how many vacation days do I get",
    "leave after having a baby",
    "remote work policy",
    "return window for unopened items",
    "restocking fee",
    "shipping lithium batteries",
    "cold-chain shipments",
    "travel expense limits",
    "lost laptop",
    "reporting a phishing email",
]
_ACCOUNT_QUERIES = ["marina", "supply", "harbor", "dock", "freight", "boat"]
_PUBLIC_DOCUMENTS = [n for n in range(1, 23) if n != 7] + [25, 26, 27, 28, 29]
_STAGES = ["prospecting", "proposal", "negotiation", "won", "lost"]
WRITE_TOOLS = frozenset(
    {
        "tickets__create_ticket",
        "tickets__add_comment",
        "tickets__change_status",
        "tickets__assign",
    }
)
_STATUSES = ["open", "pending", "resolved", "closed"]
_PRIORITIES = ["low", "normal", "high", "urgent"]
_TICKET_SUBJECTS = ["Dock gate will not latch", "Pallet label unreadable", "Carrier pickup missed"]


@dataclass(frozen=True)
class Step:
    kind: str
    """`normal`, `out_of_scope` or `auth_failure`."""
    client: str | None
    """The bot that sends it; None for an unauthenticated request."""
    tool: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    auth_case: str | None = None
    blocked_by: str = ""
    """The layer that refuses this call, when one does: `allowlist` or `rate_limit`."""
    auth_reason: str = ""
    """The reason the failed login is stored under, when it is not its case (`throttled`)."""

    @property
    def expected(self) -> tuple[str, ...]:
        """The telemetry row this step should produce, as a comparable key. A write is held for
        approval and, with the auto approver running, approved during the hold: one row."""
        if self.kind == "auth_failure":
            return ("auth_failure", self.auth_reason or self.auth_case or "")
        if self.blocked_by:
            return ("tool_call", self.client or "", self.tool or "", "blocked", self.blocked_by)
        blocked_by = (
            "catalog"
            if self.tool in UNKNOWN_TOOLS
            else "scope"
            if self.kind == "out_of_scope"
            else ""
        )
        outcome = "blocked" if self.kind == "out_of_scope" else "forwarded"
        return ("tool_call", self.client or "", self.tool or "", outcome, blocked_by)


def build_plan(seed: int, calls: int, *, writes: bool = True) -> list[Step]:
    """The sequence of calls for a seed. Pure: the same seed and size give the same plan."""
    rng = random.Random(seed)  # noqa: S311 - fictional traffic, not security
    kinds = rng.choices(list(MIX), weights=list(MIX.values()), k=calls)
    plan = []
    for kind in kinds:
        if kind == "normal":
            plan.append(_normal_step(rng, writes))
        elif kind == "out_of_scope":
            plan.append(_out_of_scope_step(rng))
        elif kind == "urgent_ticket":
            plan.append(_urgent_ticket_step(rng) if writes else _normal_step(rng, writes))
        else:
            case = rng.choices(AUTH_CASES, weights=AUTH_WEIGHTS)[0]
            plan.append(Step("auth_failure", None, auth_case=case))
    return apply_layers(plan)


def apply_layers(plan: Sequence[Step]) -> list[Step]:
    """Mark what the allowlist, the rate limit and the login throttle will do to each step.

    The gateway starts each run with these empty (the check script restarts it), so what happens
    to a step depends only on the steps before it: a support bot's urgent ticket is refused by the
    allowlist and takes no token; the first TOOL_BURSTS[tool] calls of a limited tool, per client,
    go through and the rest are refused; the first THROTTLE_PER_ID wrong secrets for the decoy id
    are 401s and the rest are 429s."""
    used: Counter[tuple[str, str]] = Counter()
    wrong_secrets = 0
    marked = []
    for step in plan:
        if step.kind == "auth_failure":
            if step.auth_case == "wrong_secret":
                wrong_secrets += 1
                if wrong_secrets > THROTTLE_PER_ID:
                    step = replace(step, auth_reason="throttled")
        elif step.kind == "normal":
            if (
                step.client == SUPPORT
                and step.tool == "tickets__create_ticket"
                and step.arguments.get("priority") == "urgent"
            ):
                step = replace(step, blocked_by="allowlist")
            elif step.tool in TOOL_BURSTS:
                key = (step.client or "", step.tool or "")
                used[key] += 1
                if used[key] > TOOL_BURSTS[step.tool or ""]:
                    step = replace(step, blocked_by="rate_limit")
        marked.append(step)
    return marked


def _normal_step(rng: random.Random, writes: bool) -> Step:
    client = rng.choice([SUPPORT, OPS])
    ticket, account = f"TKT-{rng.randint(1, 80):06d}", f"ACC-{rng.randint(1, 40):05d}"
    if writes and rng.random() < WRITE_SHARE:
        options: list[tuple[str, dict[str, Any]]] = [
            (
                "tickets__create_ticket",
                {
                    "subject": rng.choice(_TICKET_SUBJECTS),
                    "description": "Fictional demo ticket from the traffic simulator.",
                    "priority": rng.choice(_PRIORITIES),
                    "account_id": account,
                },
            ),
            (
                "tickets__add_comment",
                {"ticket_id": ticket, "body": "Simulator note: the carrier has been told."},
            ),
        ]
        if client == OPS:
            options += [
                ("tickets__change_status", {"ticket_id": ticket, "status": rng.choice(_STATUSES)}),
                ("tickets__assign", {"ticket_id": ticket, "assignee": "teddy.cormorant"}),
            ]
    else:
        options = [
            ("handbook__search", {"query": rng.choice(_SEARCHES), "limit": rng.randint(1, 5)}),
            ("handbook__get_document", {"document_id": f"DOC-{rng.choice(_PUBLIC_DOCUMENTS):03d}"}),
            ("crm__search_accounts", {"query": rng.choice(_ACCOUNT_QUERIES), "limit": 5}),
            ("crm__get_account", {"account_id": account}),
            ("crm__list_deals", {"stage": rng.choice(_STAGES), "limit": 5}),
            ("tickets__list_tickets", {"status": rng.choice(_STATUSES), "limit": 5}),
            ("tickets__get_ticket", {"ticket_id": ticket}),
        ]
    tool, arguments = rng.choice(options)
    return Step("normal", client, tool, arguments)


def _urgent_ticket_step(rng: random.Random) -> Step:
    """A support bot opening an urgent ticket: the allowlist refuses it (an ops bot's would be
    left for a person to decide)."""
    return Step(
        "normal",
        SUPPORT,
        "tickets__create_ticket",
        {
            "subject": rng.choice(_TICKET_SUBJECTS),
            "description": "Fictional demo ticket from the traffic simulator.",
            "priority": "urgent",
            "account_id": f"ACC-{rng.randint(1, 40):05d}",
        },
    )


def _out_of_scope_step(rng: random.Random) -> Step:
    if rng.random() < 0.6:
        tool = rng.choice(OPS_ONLY)
        return Step(
            "out_of_scope",
            SUPPORT,
            tool,
            {"ticket_id": f"TKT-{rng.randint(1, 80):06d}"}
            | (
                {"status": "closed"}
                if tool.endswith("change_status")
                else {"assignee": "teddy.cormorant"}
            ),
        )
    tool = rng.choice(UNKNOWN_TOOLS)
    return Step("out_of_scope", rng.choice([SUPPORT, OPS]), tool, {})


def expected_counts(plan: Sequence[Step]) -> Counter[tuple[str, ...]]:
    """What the store should hold after the plan runs: a listing per bot, plus one row per step."""
    counts: Counter[tuple[str, ...]] = Counter({("tools_list", SUPPORT): 1, ("tools_list", OPS): 1})
    counts.update(step.expected for step in plan)
    return counts


def expected_audit(plan: Sequence[Step]) -> Counter[str]:
    """What the audit log should hold for the plan, by action: a record for every call (failed
    logins are not audited), a record before every write that was forwarded, and the approval
    queue's three records (requested, resolved, consumed) for each of those writes."""
    counts: Counter[str] = Counter()
    for step in plan:
        if step.kind == "auth_failure":
            continue
        counts["gateway.tool_call"] += 1
        if step.kind == "normal" and step.tool in WRITE_TOOLS and not step.blocked_by:
            counts["gateway.call_started"] += 1
            for action in ("approval.requested", "approval.resolved", "approval.consumed"):
                counts[action] += 1
    return counts


# --- running the plan --------------------------------------------------------------------------


def _tokens(args: argparse.Namespace) -> dict[str, str]:
    if args.tokens_file:
        raw = json.loads(Path(args.tokens_file).read_text(encoding="utf-8"))
        found = {SUPPORT: raw.get(SUPPORT), OPS: raw.get(OPS), DECOY: raw.get(DECOY)}
    else:
        found = {
            SUPPORT: os.environ.get("SIM_SUPPORT_TOKEN"),
            OPS: os.environ.get("SIM_OPS_TOKEN"),
            DECOY: os.environ.get("SIM_DECOY_TOKEN"),
        }
    missing = [name for name, token in found.items() if not token]
    if missing:
        sys.exit(
            f"simulate_traffic: no token for {', '.join(missing)} (--tokens-file or SIM_*_TOKEN;"
            " the decoy is made by `gateway-admin seed-demo`)"
        )
    return {name: str(token) for name, token in found.items()}


async def _run(args: argparse.Namespace, plan: list[Step], tokens: dict[str, str]) -> None:
    rng = random.Random(args.seed + 1)  # noqa: S311 - the pacing, apart from the plan
    delay = args.duration / len(plan) if args.duration and plan else 0.0
    async with (
        _session(args.url, tokens[SUPPORT]) as support,
        _session(args.url, tokens[OPS]) as ops,
    ):
        clients = {SUPPORT: support, OPS: ops}
        for client in clients.values():
            await client.list_tools()
        for number, step in enumerate(plan, start=1):
            await _send(args.url, step, clients, tokens)
            if number % 50 == 0:
                print(f"  {number}/{len(plan)} sent")
            if delay:
                await anyio.sleep(delay * rng.uniform(0.5, 1.5))


async def _run_approved(args: argparse.Namespace, plan: list[Step], tokens: dict[str, str]) -> None:
    async with auto_approving(args.approve_as):
        await _run(args, plan, tokens)


class _Session:
    def __init__(self, url: str, token: str) -> None:
        self._http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"})
        self._client = Client(streamable_http_client(url, http_client=self._http), mode="legacy")

    async def __aenter__(self) -> Client:
        await self._http.__aenter__()
        return await self._client.__aenter__()

    async def __aexit__(self, *exc: object) -> None:
        await self._client.__aexit__(*exc)  # type: ignore[arg-type]
        await self._http.__aexit__(*exc)  # type: ignore[arg-type]


def _session(url: str, token: str) -> _Session:
    return _Session(url, token)


async def _send(url: str, step: Step, clients: dict[str, Client], tokens: dict[str, str]) -> None:
    if step.kind == "auth_failure":
        rng = random.Random(str(step))  # noqa: S311 - fictional, not security
        await _fail_authentication(
            url, step.auth_case or "", tokens, rng, 429 if step.auth_reason == "throttled" else 401
        )
        return
    assert step.client is not None
    assert step.tool is not None
    try:
        await clients[step.client].call_tool(step.tool, step.arguments)
    except MCPError:
        if step.kind != "out_of_scope" and not step.blocked_by:
            raise  # a normal call that fails is a bug in the plan, not traffic


async def _fail_authentication(
    url: str, case: str, tokens: dict[str, str], rng: random.Random, expected_status: int = 401
) -> None:
    headers = {"Accept": "application/json, text/event-stream"}
    if case == "malformed":
        headers["Authorization"] = "Bearer not-a-gateway-token"
    elif case == "unknown_token":
        alphabet = "abcdefghijklmnopqrstuvwxyz234567"
        urlsafe = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        lookup = "".join(rng.choice(alphabet) for _ in range(8))
        secret = "".join(rng.choice(urlsafe) for _ in range(43))
        headers["Authorization"] = f"Bearer aig_{lookup}_{secret}"
    elif case == "wrong_secret":
        token = tokens[DECOY]  # never a real bot's: its id is locked out after a few of these
        headers["Authorization"] = "Bearer " + token[:-1] + ("A" if token[-1] != "A" else "B")
    async with httpx2.AsyncClient() as http:
        response = await http.post(
            url, headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "ping"}
        )
    if response.status_code != expected_status:
        raise RuntimeError(
            f"an unauthenticated request got {response.status_code}, not {expected_status}"
        )


# --- verifying against what the gateway stored -------------------------------------------------


async def _stored_counts(url: str, since: datetime) -> Counter[tuple[str, ...]]:
    import psycopg  # only --verify needs a database driver

    counts: Counter[tuple[str, ...]] = Counter()
    async with await psycopg.AsyncConnection.connect(url) as connection:
        cursor = await connection.execute(
            "SELECT kind, client_name, coalesce(tool, ''), outcome, coalesce(blocked_by, '')"
            " FROM telemetry.dash_requests WHERE ts >= %s",
            (since,),
        )
        for kind, client, tool, outcome, blocked_by in await cursor.fetchall():
            counts[
                ("tools_list", client)
                if kind == "tools_list"
                else (kind, client, tool, outcome, blocked_by)
            ] += 1
        cursor = await connection.execute(
            "SELECT reason FROM telemetry.dash_auth_failures WHERE ts >= %s", (since,)
        )
        for (reason,) in await cursor.fetchall():
            counts[("auth_failure", reason)] += 1
    return counts


async def _stored_audit(url: str, since: datetime) -> Counter[str]:
    import psycopg  # only --verify needs a database driver

    counts: Counter[str] = Counter()
    async with await psycopg.AsyncConnection.connect(url) as connection:
        cursor = await connection.execute(
            "SELECT action, count(*) FROM policy.agent_core_audit WHERE occurred_at >= %s"
            " GROUP BY action",
            (since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),),
        )
        for action, count in await cursor.fetchall():
            counts[action] = count
    return counts


async def _verify_audit(plan: list[Step], since: datetime) -> bool:
    url = os.environ.get("POLICY_AUDITOR_DATABASE_URL")
    if not url:
        return True  # not asked for: the audit log is checked only when its reader is configured
    expected = expected_audit(plan)
    deadline = time.monotonic() + 20  # the batches are written within a second or so
    stored: Counter[str] = Counter()
    while time.monotonic() < deadline:
        stored = await _stored_audit(url, since)
        if stored == expected:
            print(f"verify: ok, the audit log holds {sum(expected.values())} records for the run")
            return True
        await anyio.sleep(0.5)
    print("verify: FAILED, the audit log differs from what was sent")
    for action in sorted(set(expected) | set(stored)):
        if expected[action] != stored[action]:
            print(f"  {action}: sent {expected[action]}, stored {stored[action]}")
    return False


async def _verify(plan: list[Step], since: datetime) -> bool:
    url = os.environ.get("TELEMETRY_READER_DATABASE_URL")
    if not url:
        sys.exit("simulate_traffic: --verify needs TELEMETRY_READER_DATABASE_URL")
    expected = expected_counts(plan)
    deadline = time.monotonic() + 20  # the gateway writes in the background, within a second
    stored: Counter[tuple[str, ...]] = Counter()
    while time.monotonic() < deadline:
        stored = await _stored_counts(url, since)
        if stored == expected:
            print(f"verify: ok, {sum(expected.values())} rows match what was sent")
            return await _verify_audit(plan, since)
        await anyio.sleep(0.5)
    print("verify: FAILED, what the gateway stored differs from what was sent")
    for key in sorted(set(expected) | set(stored)):
        if expected[key] != stored[key]:
            print(f"  {key}: sent {expected[key]}, stored {stored[key]}")
    return False


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", default="http://127.0.0.1:4401/mcp")
    parser.add_argument("--tokens-file", help="JSON from `gateway-admin seed-demo`")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--calls", type=int, default=300, help="requests to send (default 300)")
    parser.add_argument("--duration", type=float, default=0, help="spread the run over N seconds")
    parser.add_argument("--no-writes", action="store_true", help="read-only normal calls")
    parser.add_argument(
        "--approve-as",
        metavar="APPROVER",
        help="approve the writes as this registered approver (test tooling; needs"
        " LAB_AUTO_APPROVE=yes and POLICY_APPROVER_DATABASE_URL). Without it, run with --no-writes",
    )
    parser.add_argument("--verify", action="store_true", help="check the stored telemetry")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    if args.calls < 1:
        sys.exit("simulate_traffic: --calls must be at least 1")
    if not args.no_writes and not args.approve_as:
        sys.exit("simulate_traffic: writes wait for a person; use --approve-as or --no-writes")
    plan = build_plan(args.seed, args.calls, writes=not args.no_writes)
    tokens = _tokens(args)
    mix = Counter(step.kind for step in plan)
    print(f"seed {args.seed}: {args.calls} requests {dict(mix)}")
    started = datetime.now(UTC) - timedelta(seconds=2)
    anyio.run(_run_approved, args, plan, tokens)
    print(f"sent {len(plan)} requests")
    if args.verify and not anyio.run(_verify, plan, started):
        sys.exit(1)


if __name__ == "__main__":
    main()
