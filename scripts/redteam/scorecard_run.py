"""Run the red-team scorecard: every attack, against the lab gateway, once for each column.

    make scorecard                      # a fresh lab stack, every column, replay mode, no API key
    uv run scripts/redteam/scorecard_run.py --check     # regenerate, and fail if the committed
                                                        # deterministic part differs

The lab stack is its own Compose project (`ai-gateway-scorecard`, a fresh volume, removed at the
end), never the real one. For each of the columns (`lab_config.COLUMNS`) the gateway is restarted
once, on that column's generated pipeline; each attack then runs as a client of its own (a clone of
the attack's original, `config/lab/clients.lab.toml`), those that cannot disturb each other at the
same time. Success is the independent oracle's verdict, never the gateway's own record. The
classifier answers from committed recordings (replay mode), so nothing needs a key and nothing is
billed. It uses only 04's ports, takes the Docker lock for one command at a time, and prints no
token or customer value. It starts the lab approver, which approves (or, in one column, rejects)
every write, and the lab upstream: both test tooling that must never touch anything real.
Harborline Supply Co. is fictional.
"""

import argparse
import json
import os
import secrets
import sys
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

import anyio

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from redteam.attack_format import LAB_PHASES, Attack, load_attack
from redteam.execute import Ran, run_attack_once
from redteam.lab_config import COLUMNS, Column
from redteam.oracle import Evidence, attack_succeeded, restore_snapshot, take_snapshot
from redteam.prediction import CONFIG, load_effects, predicted_success
from redteam.scorecard import (
    AttackInfo,
    Observation,
    build,
)
from redteam.scorecard_render import render_all
from redteam.stack import (
    Stack,
    StackError,
    clean_environment,
    read_env_file,
    redact,
    run,
)

ROOT = Path(__file__).resolve().parents[2]
ATTACKS = ROOT / "scripts" / "redteam" / "attacks"
DOCS = ROOT / "docs"
PROJECT = "ai-gateway-scorecard"
GATEWAY_URL = "http://127.0.0.1:4401/mcp"
CATALOG_REFRESH_S = 3
CONCURRENCY = 6
LAB_CONFIG = "/app/config/lab"


# -- what a column asks of Compose ------------------------------------------------------------


def column_env(column: Column) -> dict[str, str]:
    """The environment that points the lab gateway (and the lab approver) at one column."""
    classifier = (
        "/app/config/classifier.toml"
        if column.classifier == "current"
        else f"{LAB_CONFIG}/classifier.{column.classifier}.toml"
    )
    return {
        "GATEWAY_PIPELINE_FILE_IN_CONTAINER": f"{LAB_CONFIG}/pipelines/{column.id}.toml",
        "GATEWAY_ALLOWLIST_FILE_IN_CONTAINER": f"{LAB_CONFIG}/allowlist.lab.toml",
        "GATEWAY_TOOL_PINS_FILE_IN_CONTAINER": f"{LAB_CONFIG}/tool_pins.lab.toml",
        "GATEWAY_APPROVAL_ROLES_FILE_IN_CONTAINER": f"{LAB_CONFIG}/approval_roles.lab.toml",
        "GATEWAY_CLASSIFIER_FILE_IN_CONTAINER": classifier,
        "GATEWAY_CATALOG_REFRESH_S": str(CATALOG_REFRESH_S),
        "LAB_APPROVER_DECISION": column.approver,
    }


def owner_url(env_file: Mapping[str, str]) -> str:
    """The database owner's URL on 04's port, with the user and password quoted."""
    user = quote(env_file["POSTGRES_USER"], safe="")
    password = quote(env_file["POSTGRES_PASSWORD"], safe="")
    return f"postgresql://{user}:{password}@127.0.0.1:4402/{env_file['POSTGRES_DB']}"


def require_committed(path: Path) -> None:
    """A check against an uncommitted scorecard cannot pass: say so before a stack starts."""
    if not path.is_file():
        sys.exit(f"scorecard_run: {path} is not committed yet: run `make scorecard` first")


def approver_env() -> dict[str, str]:
    """What the lab approver needs for the whole run: the lab roles file, so it can decide the lab
    upstream's write as well as the product's."""
    return {"LAB_APPROVAL_ROLES_FILE_IN_CONTAINER": f"{LAB_CONFIG}/approval_roles.lab.toml"}


# -- which attacks may run at the same time -------------------------------------------------------


@dataclass
class Plan:
    product: list[Attack] = field(default_factory=list)
    """Attacks on the product's tools whose success the oracle reads from their own tickets: they
    run at once, each as a client of its own."""
    snapshot: list[Attack] = field(default_factory=list)
    """Attacks judged by a diff of the ticketing data: each runs alone, after the rest, and the
    data is restored after it."""
    lab: dict[str, list[Attack]] = field(default_factory=dict)
    """Attacks on the lab upstream, by the phase it must be in: the phases run in turn, the
    attacks of one phase at once (the upstream counts calls for each client)."""


def plan(attacks: Iterable[Attack]) -> Plan:
    made = Plan(lab={phase: [] for phase in LAB_PHASES})
    for attack in attacks:
        if attack.lab_phase is not None:
            made.lab[attack.lab_phase].append(attack)
        elif attack.expect.oracle == "unauthorized-write":
            made.snapshot.append(attack)
        else:
            made.product.append(attack)
    made.lab = {phase: group for phase, group in made.lab.items() if group}
    return made


# -- turning a run into an observation ------------------------------------------------------------


def predict(column: Column, attack: Attack, effects: Mapping[str, str]) -> bool:
    """What the attack file predicts for the column: the layers it weakens (off or only watching)
    and the approver it uses. The before columns weaken no layer, so they predict what all-on does
    and the flags show as differences."""
    weakened = frozenset(layer for layer, mode in column.modes.items() if mode != "enforce")
    approver = "deny" if column.approver == "reject" else "allow"
    return predicted_success(attack, weakened, effects, approver)


def lab_effects_of(attack: Attack, client: str, snapshot: Mapping[str, Any]) -> int:
    """How many calls of the attack's landing tools the lab upstream executed for this client."""
    calls = snapshot.get("by_client", {}).get(client, {}).get("calls", {})
    landing = {
        step.tool.removeprefix("lab__") for step in attack.steps if step.id in attack.expect.landing
    }
    return sum(count for tool, count in calls.items() if tool in landing)


def observe(
    column: Column,
    attack: Attack,
    ran: Ran,
    *,
    lab_effects: int | None,
    effects: Mapping[str, str],
) -> Observation:
    answered = sum(r.outcome == "answered" for r in ran.records if r.step in attack.expect.landing)
    evidence = Evidence(ran.landed, ran.changes, answered, lab_effects)
    success = attack_succeeded(attack.expect, evidence) if attack.hostile else False
    blocked: Counter[str] = Counter(
        row["blocked_by"] for row in ran.rows if row["outcome"] == "blocked" and row["blocked_by"]
    )
    would: Counter[str] = Counter(
        layer
        for row in ran.rows
        for layer, verdict in row["verdicts"].items()
        if verdict == "would_block"
    )
    unclassified = sum("unclassified" in row["verdicts"].values() for row in ran.rows)
    return Observation(
        success=success,
        predicted=predict(column, attack, effects),
        calls=len(ran.rows),
        blocked_by=dict(blocked),
        would_block=dict(would),
        unclassified=unclassified,
        incomplete=ran.incomplete,
    )


def attack_info(attack: Attack) -> AttackInfo:
    catchers = sorted(
        {
            c
            for step in attack.steps
            for e in step.enforce
            if e.outcome == "blocked"
            for c in e.catchers
        }
    )
    return AttackInfo(
        id=attack.id,
        title=attack.title,
        family=attack.expect.family,
        client=attack.client,
        oracle=attack.expect.oracle,
        threshold=attack.expect.threshold,
        hostile=attack.hostile,
        gap=attack.expect.gap,
        expected_catchers=tuple(catchers),
        lab_phase=attack.lab_phase,
    )


def load_attacks(only: Sequence[str] | None = None) -> list[Attack]:
    found = [load_attack(path) for path in sorted(ATTACKS.glob("*.toml"))]
    return [a for a in found if not only or a.id in only]


# -- the run ----------------------------------------------------------------------------------


@dataclass
class Context:
    stack: Stack
    owner_url: str
    reader_url: str
    tokens: dict[str, str]
    effects: Mapping[str, str]
    latency: dict[str, list[float]] = field(default_factory=dict)
    results: dict[str, dict[str, Observation]] = field(default_factory=dict)
    approver: str = "approve"
    """What the running lab approver does with a write: the stack starts it approving."""
    approver_ready: bool = False


def clone_of(attack: Attack) -> str:
    return f"{attack.client}--{attack.id}"


async def _one(
    ctx: Context, column: Column, attack: Attack, lab_effects: Mapping[str, Any] | None = None
) -> Ran:
    ran = await run_attack_once(
        attack,
        gateway_url=GATEWAY_URL,
        token=ctx.tokens[clone_of(attack)],
        client_name=clone_of(attack),
        owner_url=ctx.owner_url,
        reader_url=ctx.reader_url,
    )
    if not attack.hostile:
        ctx.latency.setdefault(column.id, []).extend(row["duration_ms"] for row in ran.rows)
    return ran


async def _concurrently(ctx: Context, column: Column, attacks: Sequence[Attack]) -> dict[str, Ran]:
    limiter = anyio.CapacityLimiter(CONCURRENCY)
    ran: dict[str, Ran] = {}

    async def go(attack: Attack) -> None:
        async with limiter:
            ran[attack.id] = await _one(ctx, column, attack)

    async with anyio.create_task_group() as tasks:
        for attack in attacks:
            tasks.start_soon(go, attack)
    return ran


async def run_column(ctx: Context, column: Column, attacks: Sequence[Attack]) -> None:
    """Restart the lab gateway on the column, run every attack, restore the data."""
    print(f"== column {column.id}: {column.label}", flush=True)
    env = column_env(column)
    stack = ctx.stack
    if column.approver != ctx.approver:
        # Only the approver: its dependency, policy-setup, would reset the lab role's grants.
        await stack.compose("up", "-d", "--force-recreate", "--no-deps", "lab-approver", extra=env)
        ctx.approver = column.approver
        ctx.approver_ready = False
    if not ctx.approver_ready:
        await stack.wait_for_approver(ctx.approver)
        ctx.approver_ready = True
    # Only the gateway: its dependencies (migrate, the setups) would run again and reset the roles.
    await stack.compose("up", "-d", "--force-recreate", "--no-deps", "--wait", "gateway", extra=env)
    await _wait_for_gateway(ctx)
    snapshot = await anyio.to_thread.run_sync(take_snapshot, ctx.owner_url)
    made = plan(attacks)
    observations: dict[str, Observation] = {}

    def record(attack: Attack, ran: Ran, lab: int | None = None) -> None:
        observations[attack.id] = observe(column, attack, ran, lab_effects=lab, effects=ctx.effects)

    ran_product = await _concurrently(ctx, column, made.product)
    for attack in made.product:
        record(attack, ran_product[attack.id])
    for attack in made.snapshot:
        record(attack, await _one(ctx, column, attack))
        await anyio.to_thread.run_sync(restore_snapshot, ctx.owner_url, snapshot)
    for phase, group in made.lab.items():
        await stack.set_lab_phase(phase)
        await anyio.sleep(2 * CATALOG_REFRESH_S + 1)  # the catalogue sees the new definitions
        ran_group = await _concurrently(ctx, column, group)
        counts = await stack.lab_effects()
        for attack in group:
            record(attack, ran_group[attack.id], lab_effects_of(attack, clone_of(attack), counts))
    await anyio.to_thread.run_sync(restore_snapshot, ctx.owner_url, snapshot)
    ctx.results[column.id] = observations
    wins = sum(o.success for a in attacks if a.hostile for o in [observations[a.id]])
    print(f"   {wins} of {sum(a.hostile for a in attacks)} hostile attacks succeeded", flush=True)


async def _wait_for_gateway(ctx: Context) -> None:
    """The gateway answers and offers a lab client the lab tools (the catalogue has loaded)."""
    from redteam.scripted_client import connect

    name = next(n for n in ctx.tokens if n.startswith("harborline-lab-bot--"))
    last = "no answer"
    for _ in range(60):
        try:
            async with connect(GATEWAY_URL, ctx.tokens[name]) as client:
                if any(t.name.startswith("lab__") for t in (await client.list_tools()).tools):
                    return
                last = "answered, but offered no lab tool"
        except Exception as error:
            last = type(error).__name__
        await anyio.sleep(2)
    raise StackError(f"the lab gateway never offered the lab tools ({last})")


SWITCHES = ("LAB_AUTO_APPROVE", "LAB_MUTABLE_UPSTREAM", "LAB_FLOOR_OVERRIDE")


def require_switches(env: Mapping[str, str]) -> None:
    """The three lab switches are the caller's to turn on, as for every lab script: this refuses
    to start unless each is exactly `yes` in its environment, and sets none. They start the lab
    approver and the lab upstream and let two columns weaken a floor layer: test tooling that
    defeats or abuses a control on purpose, on a fictional stack."""
    if any(env.get(name) != "yes" for name in SWITCHES):
        given = " ".join(f"{name}=yes" for name in SWITCHES)
        sys.exit(
            "scorecard_run: this starts the lab approver and the lab upstream and weakens floor "
            f"layers in two columns, on a fictional stack. Run it with {given} in front, e.g. "
            f"`{given} make scorecard`."
        )


async def main_async(args: argparse.Namespace) -> int:
    require_switches(os.environ)
    if args.check:
        require_committed(DOCS / "scorecard.json")
    attacks = load_attacks(args.attacks)
    columns = [c for c in COLUMNS if not args.columns or c.id in args.columns]
    full = not args.attacks and not args.columns
    env_file = read_env_file(ROOT / ".env")
    base = clean_environment()
    base.update(
        {
            "LAB_UPSTREAM_TOKEN": secrets.token_urlsafe(40),
            "POLICY_LAB_APPROVER_DB_PASSWORD": secrets.token_urlsafe(30),
            "GATEWAY_MIGRATE_DATABASE_URL": env_file["GATEWAY_MIGRATE_DATABASE_URL"],
            **approver_env(),
        }
    )
    stack = Stack(ROOT, PROJECT, base)
    owner = owner_url(env_file)
    ctx = Context(
        stack=stack,
        owner_url=owner,
        reader_url=env_file["TELEMETRY_READER_DATABASE_URL"],
        tokens={},
        effects=load_effects(
            CONFIG / "tool_policies.toml", CONFIG / "lab" / "tool_policies.lab.toml"
        ),
    )
    try:
        print("== building and starting the lab stack", flush=True)
        if not args.no_build:
            await stack.compose("build", *args.build)
        # `up` also reconciles a stack left running by an earlier run: what a new run's credentials
        # change (the lab upstream, the gateway, the one-shot that makes the lab approver's role) is
        # recreated.
        await stack.compose("up", "-d", "--wait")
        print("== registering the lab upstream and a client for each attack", flush=True)
        await run(["uv", "run", "gateway-admin", "seed-demo"], base, ROOT)
        out = await run(["uv", "run", "gateway-admin", "seed-lab"], base, ROOT)
        ctx.tokens = json.loads(out[out.index("{") :])
        for column in columns:
            await run_column(ctx, column, attacks)
        if not full:
            _print_partial(ctx.results, attacks)
            return 0
        card = build(COLUMNS, [attack_info(a) for a in attacks], ctx.results, ctx.latency)
        return _finish(card, args)
    finally:
        if not args.no_down:
            try:
                await stack.compose("down", "-v", "--remove-orphans")
            except StackError as error:
                print(f"(the stack could not be removed: {redact(str(error))})", file=sys.stderr)


def _print_partial(
    results: Mapping[str, Mapping[str, Observation]], attacks: Sequence[Attack]
) -> None:
    """A partial run writes nothing: it prints what each attack did, observed and predicted."""
    print("(a partial run: nothing is written or checked)")
    for column, rows in results.items():
        for attack in attacks:
            o = rows[attack.id]
            flag = "" if o.success == o.predicted else "   <-- differs from the prediction"
            print(
                f"  {column:18} {attack.id:34} success={o.success!s:5} predicted={o.predicted!s:5} "
                f"blocked={dict(o.blocked_by)} would_block={dict(o.would_block)} "
                f"unclassified={o.unclassified}{' INCOMPLETE' if o.incomplete else ''}{flag}"
            )


def _finish(card: dict[str, Any], args: argparse.Namespace) -> int:
    text = json.dumps(card, indent=2, ensure_ascii=False) + "\n"
    if args.check:
        committed = json.loads((DOCS / "scorecard.json").read_text(encoding="utf-8"))
        if committed["deterministic"] != card["deterministic"]:
            print(
                "FAIL  the committed scorecard's deterministic part differs from this run",
                file=sys.stderr,
            )
            return 1
        print("ok    the committed scorecard still matches")
        return 0
    (DOCS / "scorecard.json").write_text(text, encoding="utf-8")
    render_all(DOCS, ROOT / "README.md")
    print("wrote docs/scorecard.json, docs/scorecard.md, the chart and the README section")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--check", action="store_true", help="fail if the committed scorecard differs"
    )
    parser.add_argument(
        "--columns", nargs="*", help="only these columns (a partial run writes nothing)"
    )
    parser.add_argument(
        "--attacks", nargs="*", help="only these attacks (a partial run writes nothing)"
    )
    parser.add_argument(
        "--build",
        nargs="*",
        default=[],
        metavar="SERVICE",
        help="build only these services' images (default: all, which retags the shared ones)",
    )
    parser.add_argument("--no-build", action="store_true", help="do not build any image")
    parser.add_argument("--no-down", action="store_true", help="leave the lab stack up afterwards")
    args = parser.parse_args()
    sys.exit(anyio.run(main_async, args))


if __name__ == "__main__":
    main()
