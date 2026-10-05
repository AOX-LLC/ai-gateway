"""Record the injection classifier's judgements, or count what is missing, or verify nothing is.

    uv run scripts/record_classifier.py --count      # what would be recorded, and what it costs
    AGENT_CORE_MODE=record uv run scripts/record_classifier.py     # record the missing ones
    uv run scripts/record_classifier.py --verify     # replay every unit: zero misses or it fails

`--count` and `--verify` run in replay mode: no key, no network, nothing spent. Recording needs a
person's API key in `AGENT_CORE_ANTHROPIC_API_KEY` (agent-core reads it itself; this script never
reads, prints or logs it) and `AGENT_CORE_MODE=record`; it records only the units that have no
recording yet, so a second run costs nothing, and writes under `config/recordings/`, which is
committed. It prints counts, tokens and cost, never a text. Harborline Supply Co. is fictional.
"""

import argparse
import asyncio
import os
import secrets
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

from aox_agent_core import AgentClient, Tier, load_config
from aox_agent_core.errors import ReplayMissError

sys.path.insert(0, str(Path(__file__).resolve().parent))
import classifier_corpus as corpus
from ai_gateway.classifier.judge import Judge, Outcome
from ai_gateway.classifier.prompt import JUDGE, Judgement, prepare
from ai_gateway.hashing import configure_hash_key

CONFIG_FILE = corpus.ROOT / "config" / "agent-core.toml"
CHARS_PER_TOKEN = 3.5
"""A deliberately cautious estimate (English averages about 4): the real count comes back with each
call, and the cost printed after recording is the real one."""
OUTPUT_TOKENS = 30
"""An enum-only answer is about twenty tokens."""


def _client(mode: str) -> AgentClient:
    environ = dict(os.environ)
    environ["AGENT_CORE_MODE"] = mode
    return AgentClient(load_config(CONFIG_FILE, environ=environ))


async def _missing(units: list[corpus.CorpusUnit]) -> list[corpus.CorpusUnit]:
    """The units replay mode has no recording for (nothing is called, nothing is spent)."""
    client = _client("replay")
    missing = []
    for unit in units:
        try:
            await client.call(
                JUDGE,
                inputs={"surface": unit.surface, "text": prepare(unit.text)},
                output=Judgement,
                tier=_tier(),
                max_tokens=64,
            )
        except ReplayMissError:
            missing.append(unit)
    return missing


def _tier() -> Tier:
    return Tier.SMALL


def _estimate(units: list[corpus.CorpusUnit]) -> tuple[int, int, Decimal, str]:
    config = load_config(CONFIG_FILE, environ={})
    tier = config.routing.tiers[_tier()]
    price = config.pricing[tier.provider].models[tier.model]
    overhead = len(JUDGE.system or "") + len(JUDGE.template)
    tokens_in = sum(int((overhead + len(prepare(u.text))) / CHARS_PER_TOKEN) + 1 for u in units)
    tokens_out = OUTPUT_TOKENS * len(units)
    cost = (
        Decimal(tokens_in) * price.input_usd_per_mtok
        + Decimal(tokens_out) * price.output_usd_per_mtok
    ) / Decimal(1_000_000)
    return tokens_in, tokens_out, cost, tier.model


async def _count(units: list[corpus.CorpusUnit]) -> int:
    missing = await _missing(units)
    must = corpus.distinct(corpus.must_cover())
    must_missing = [
        u for u in missing if (u.surface, u.text) in {(m.surface, m.text) for m in must}
    ]
    tokens_in, tokens_out, cost, model = _estimate(missing)
    print(f"units in the corpus (distinct texts): {len(units)}")
    print(f"  attack corpus, benign look-alikes and the 09 story: {len(must)}")
    red = {(u.surface, u.text) for u in [*corpus.redteam_corpus(), *corpus.attack_strings()]}
    red_missing = [u for u in missing if (u.surface, u.text) in red]
    print(f"  the red-team scorecard's strings: {len(red)} ({len(red_missing)} not yet recorded)")
    print(f"  recorded already: {len(units) - len(missing)}")
    print(f"live calls needed: {len(missing)}  (must-cover groups: {len(must_missing)})")
    print(f"model: {model} (small tier), one call per unit, answer is an enum")
    print(f"estimated tokens: about {tokens_in} in and {tokens_out} out")
    print(
        f"estimated cost: about ${cost:.2f} (a cautious estimate; the real cost is printed after)"
    )
    return len(missing)


async def _record(units: list[corpus.CorpusUnit]) -> None:
    if os.environ.get("AGENT_CORE_MODE") != "record":
        sys.exit(
            "record_classifier: set AGENT_CORE_MODE=record"
            " (and AGENT_CORE_ANTHROPIC_API_KEY) to record"
        )
    if not os.environ.get("AGENT_CORE_ANTHROPIC_API_KEY"):
        sys.exit("record_classifier: AGENT_CORE_ANTHROPIC_API_KEY is not set")
    missing = await _missing(units)
    print(f"{len(missing)} units to record")
    totals: dict[str, Any] = {"in": 0, "out": 0, "cost": Decimal(0), "ok": 0, "failed": 0}
    client = _client("record")

    def book(record) -> None:  # type: ignore[no-untyped-def]
        totals["in"] += record.input_tokens
        totals["out"] += record.output_tokens
        totals["cost"] += record.cost_usd

    judge = Judge(client, corpus.judge_config(), book)
    judge.config = judge.config.__class__(
        **{**judge.config.__dict__, "max_calls_per_minute_per_client": 10_000}
    )
    sem = asyncio.Semaphore(4)

    async def one(unit: corpus.CorpusUnit) -> None:
        async with sem:
            result = await judge.judge(
                unit.surface, unit.text, client_name="recorder", request_id=None
            )
        totals["ok" if result.outcome is not Outcome.FAILED else "failed"] += 1

    await asyncio.gather(*(one(unit) for unit in missing))
    print(f"recorded {totals['ok']}, failed {totals['failed']}")
    print(f"tokens: {totals['in']} in, {totals['out']} out; cost ${totals['cost']:.4f}")
    if totals["failed"]:
        sys.exit("some units failed: run it again (only the missing ones are recorded)")


async def _verify(units: list[corpus.CorpusUnit]) -> None:
    missing = await _missing(units)
    must = {(u.surface, u.text) for u in corpus.must_cover()}
    print(f"{len(units) - len(missing)} of {len(units)} units have a recording")
    if missing:
        required = sum((u.surface, u.text) in must for u in missing)
        print(f"MISSING: {len(missing)} ({required} must-cover)")
        for unit in missing[:20]:
            print(f"  - {unit.source} {unit.item_id or ''} [{unit.surface}] {len(unit.text)} chars")
        sys.exit(1)
    client = _client("replay")
    judge = Judge(client, corpus.judge_config(), None)
    judge.config = judge.config.__class__(
        **{**judge.config.__dict__, "max_calls_per_minute_per_client": 10_000}
    )
    classifier_misses = false_positives = caught = clean_ok = 0
    elsewhere: dict[str, list[str]] = {}
    for unit in corpus.must_cover():
        got = (
            await judge.judge(unit.surface, unit.text, client_name="verify", request_id=None)
        ).outcome
        flagged = got is Outcome.INJECTION
        if unit.expect == "clean":
            if flagged:
                false_positives += 1
                print(f"  false positive: {unit.source} {unit.item_id}")
            else:
                clean_ok += 1
        elif "classifier" in unit.catchers:
            if flagged:
                caught += 1
            else:
                classifier_misses += 1
                print(f"  classifier miss: {unit.source} {unit.item_id}")
        else:
            # Not the classifier's to catch (the text holds no instruction to a model): reported
            # under the layers expected to catch it, neither a hit nor a miss for the classifier.
            for layer in unit.catchers:
                elsewhere.setdefault(layer, []).append(f"{unit.source} {unit.item_id}")
    print(
        f"classifier: caught {caught}, missed {classifier_misses} of the items it is expected to"
        f" catch; {false_positives} false positives on {false_positives + clean_ok} clean items"
    )
    for layer, items in sorted(elsewhere.items()):
        print(f"expected of {layer}, not of the classifier ({len(items)}): " + ", ".join(items))
    await _report_red_team(judge)
    if classifier_misses or false_positives:
        sys.exit(1)


async def _report_red_team(judge: Judge) -> None:
    """What the judge makes of each red-team item, under v0.1.0's rule and the current one. This is
    reported and never fails the run: how the classifier does on them is the scorecard's finding."""
    config = corpus.judge_config()
    legacy, current = corpus.both_rules(config)
    print("red-team items (a finding for the scorecard, not a pass or fail):")
    for item in corpus.redteam_items():
        verdicts = []
        for name, rule in (("v0.1.0 rule", legacy), ("current rule", current)):
            units = corpus.units_of_text(item.text, rule)
            if not units:
                verdicts.append(f"{name}: not judged")
                continue
            outcomes = [
                (
                    await judge.judge(item.surface, unit, client_name="verify", request_id=None)
                ).outcome
                for unit in units
            ]
            flagged = sum(o is Outcome.INJECTION for o in outcomes)
            verdicts.append(f"{name}: {flagged} of {len(units)} units flagged")
        print(f"  {item.item_id} (expect {item.expect}): " + "; ".join(verdicts))


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--count", action="store_true", help="how many calls, and the cost")
    group.add_argument("--verify", action="store_true", help="replay every unit: fail on a miss")
    args = parser.parse_args()
    # The judge's cache is keyed by a hash of each text. Here it only has to agree with itself for
    # one run, so the key is random and thrown away: the gateway's own key is never needed or read.
    configure_hash_key(secrets.token_bytes(32))
    units = corpus.everything()
    if args.count:
        asyncio.run(_count(units))
    elif args.verify:
        asyncio.run(_verify(units))
    else:
        asyncio.run(_record(units))


if __name__ == "__main__":
    main()
