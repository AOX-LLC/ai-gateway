"""Every text the injection classifier's committed recordings must cover.

The classifier runs in replay mode by default (no key, no network): it serves a recording for each
unit of text and calls anything it has no recording for *unclassified*. This module lists the texts
that must never be unclassified, in two groups:

- **must_cover**: the attack corpus (`config/classifier_corpus/attacks.toml`), its benign
  look-alikes, and the integrated demo's story strings (`story_09.toml`). A test fails if any of
  them has no recording, so Phase 6's scorecard and project 09's demo never meet a miss.
- **seeded_data**: the free text of the fictional seed data, of the Harborline scenario's writes
  and of the traffic simulator's writes, so the stack's own checks run with nothing unclassified.

`scripts/record_classifier.py` records what is missing (with a key, by a person); `--count` says how
many calls that is and what it will cost, before anything is spent.
"""

import importlib.util
import sys
import tomllib
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ai_gateway.classifier.judge import JudgeConfig
from ai_gateway.classifier.prompt import SURFACE_ARGUMENTS, SURFACE_RESULT, units_of
from ai_gateway.pipeline.layers.classifier import load_judge_config

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "config" / "classifier_corpus"
WRITE_TOOLS = {"create_ticket", "add_comment", "change_status", "assign"}
SIMULATOR_RUNS = ((200, 1), (60, 7))
"""(calls, seed) of the simulator runs scripts/run_harborline_check.sh makes."""


@dataclass(frozen=True)
class CorpusUnit:
    source: str
    surface: str
    text: str
    expect: str | None = None
    item_id: str | None = None
    catchers: tuple[str, ...] = ()
    """The layers expected to catch the item (see config/classifier_corpus/README.md)."""


def judge_config() -> JudgeConfig:
    return load_judge_config(ROOT / "config" / "classifier.toml")


def _items(file: str) -> list[dict[str, Any]]:
    raw = tomllib.loads((CORPUS / file).read_text(encoding="utf-8"))
    items: list[dict[str, Any]] = raw["item"]
    return items


def must_cover() -> list[CorpusUnit]:
    """Attack corpus, benign look-alikes and the 09 story: each item is one unit as written (a
    corpus text is a single value, so it is not cut or filtered)."""
    return [
        CorpusUnit(
            file, item["surface"], item["text"], item["expect"], item["id"], _catchers(file, item)
        )
        for file in ("attacks.toml", "benign.toml", "story_09.toml")
        for item in _items(file)
    ]


CATCHER_LAYERS = frozenset({"classifier", "egress", "canary", "schema", "approval"})


def _catchers(file: str, item: dict[str, Any]) -> tuple[str, ...]:
    """The item's expected catching layers. A hostile item has some and a clean one has none, so an
    item cannot be left unscored by forgetting the field."""
    where = f"{file} {item['id']}"
    catchers = item.get("catchers")
    if not isinstance(catchers, list) or not set(catchers) <= CATCHER_LAYERS:
        raise ValueError(f"{where}: catchers must be a list of {sorted(CATCHER_LAYERS)}")
    if (item["expect"] == "injection") != bool(catchers):
        raise ValueError(f"{where}: a hostile item needs catchers and a clean one has none")
    return tuple(catchers)


def _units(value: Any, config: JudgeConfig) -> list[str]:
    return units_of(
        value,
        min_chars=config.min_chars,
        min_words=config.min_words,
        max_chars=config.max_unit_chars,
    )


def _row_values(row: Any) -> dict[str, Any]:
    """A seed row's fields as a tool returns them: nothing the databases keep to themselves."""
    return {k: v for k, v in asdict(row).items() if "internal" not in k and isinstance(v, str)}


def seeded_data(config: JudgeConfig | None = None) -> list[CorpusUnit]:
    from crm_server.seed import build_dataset as crm
    from harborline_setup.handbook_documents import load_documents
    from mcp_common.notice import FICTIONAL_NOTICE
    from ticketing_server.seed import build_dataset as ticketing

    config = config or judge_config()
    units: list[CorpusUnit] = [CorpusUnit("notice", SURFACE_RESULT, FICTIONAL_NOTICE)]
    crm_data = crm()
    for source, rows in (
        ("crm accounts", crm_data.accounts),
        ("crm contacts", crm_data.contacts),
        ("crm deals", crm_data.deals),
        ("crm notes", crm_data.notes),
    ):
        units += [
            CorpusUnit(source, SURFACE_RESULT, text)
            for row in rows
            for text in _units(_row_values(row), config)
        ]
    tickets = ticketing()
    units += [
        CorpusUnit("tickets", SURFACE_RESULT, text)
        for ticket in tickets.tickets
        for text in _units(_row_values(ticket), config)
    ]
    units += [
        CorpusUnit("ticket comments", SURFACE_RESULT, text)
        for comment in tickets.comments
        if comment.visibility == "public"
        for text in _units(_row_values(comment), config)
    ]
    for document in load_documents(ROOT / "servers" / "handbook" / "documents"):
        if document.classification != "restricted":
            units += [
                CorpusUnit("handbook", SURFACE_RESULT, text)
                for text in _units([document.title, document.body], config)
            ]
    units += scenario_writes(config) + simulator_writes(config)
    return units


_WRITTEN_SURFACES = (SURFACE_ARGUMENTS, SURFACE_RESULT)
"""Text a client writes is judged as arguments, and a later read returns it (a ticket's body, a
comment) and judges it again as a result: both are recorded."""


def scenario_writes(config: JudgeConfig) -> list[CorpusUnit]:
    raw = tomllib.loads((ROOT / "scripts" / "scenarios" / "harborline.toml").read_text())
    return [
        CorpusUnit("scenario", surface, text)
        for call in raw["call"]
        if call["tool"] in WRITE_TOOLS
        for text in _units(call["arguments"], config)
        for surface in _WRITTEN_SURFACES
    ]


def simulator_writes(config: JudgeConfig) -> list[CorpusUnit]:
    scripts = str(ROOT / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    spec = importlib.util.spec_from_file_location(
        "simulate_traffic", ROOT / "scripts" / "simulate_traffic.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    units: list[CorpusUnit] = []
    for calls, seed in SIMULATOR_RUNS:
        for step in module.build_plan(seed, calls, writes=True):
            if step.kind == "normal" and (step.tool or "").split("__")[-1] in WRITE_TOOLS:
                units += [
                    CorpusUnit("simulator", surface, t)
                    for t in _units(step.arguments, config)
                    for surface in _WRITTEN_SURFACES
                ]
    return units


def distinct(units: Iterable[CorpusUnit]) -> list[CorpusUnit]:
    """One unit per (surface, text), the first source first: a text is recorded once."""
    seen: set[tuple[str, str]] = set()
    out: list[CorpusUnit] = []
    for unit in units:
        key = (unit.surface, unit.text)
        if key not in seen:
            seen.add(key)
            out.append(unit)
    return out


def everything() -> list[CorpusUnit]:
    return distinct([*must_cover(), *seeded_data()])
