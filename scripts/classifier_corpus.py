"""Every text the injection classifier's committed recordings must cover.

The classifier runs in replay mode by default (no key, no network): it serves a recording for each
unit of text and calls anything it has no recording for *unclassified*. This module lists the texts
that must never be unclassified, in two groups:

- **must_cover**: the attack corpus (`config/classifier_corpus/attacks.toml`), its benign
  look-alikes, and the integrated demo's story strings (`story_09.toml`). A test fails if any of
  them has no recording, so Phase 6's scorecard and project 09's demo never meet a miss.
- **red-team strings** (`redteam_corpus`, `attack_strings`): the scorecard's own,
  `config/classifier_corpus/redteam.toml` (obfuscated instructions, what the lab upstream says),
  and every static text the attack files write or plant (a text with a template that is filled from
  what an attack read cannot be known here, and is data only). Each is recorded as the units both
  the old and the current rule cut it into, so the scorecard can show before and after without a
  miss.
- **seeded_data**: the free text of the fictional seed data, of the Harborline scenario's writes
  and of the traffic simulator's writes, so the stack's own checks run with nothing unclassified.

`scripts/record_classifier.py` records what is missing (with a key, by a person); `--count` says how
many calls that is and what it will cost, before anything is spent.
"""

import dataclasses
import importlib
import importlib.util
import json
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
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))
CORPUS = ROOT / "config" / "classifier_corpus"
ATTACKS = ROOT / "scripts" / "redteam" / "attacks"
REDTEAM = "redteam.toml"
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
        overlap=config.unit_overlap_chars,
        normalize_separators=config.short_text == "normalized",
    )


def _row_values(row: Any) -> dict[str, Any]:
    """A seed row's fields as a tool returns them: nothing the databases keep to themselves."""
    return {k: v for k, v in asdict(row).items() if "internal" not in k and isinstance(v, str)}


def seeded_data(config: JudgeConfig | None = None) -> list[CorpusUnit]:
    from crm_server.seed import build_dataset as crm
    from handbook_server.repo import snippet
    from harborline_setup.handbook_documents import chunk_document, load_documents
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
            # Search returns each chunk's heading and a snippet of its text, not the whole body.
            units += [
                CorpusUnit("handbook search", SURFACE_RESULT, text)
                for chunk in chunk_document(document)
                for text in _units([chunk.heading, snippet(chunk.text)], config)
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


def units_of_text(text: str, config: JudgeConfig) -> list[str]:
    """The units the classifier judges in one value, under a configuration."""
    return _units(text, config)


def _text_of(item: dict[str, Any]) -> str:
    """An item's text: given, or built (`builder = "module:name"`, a string or a function)."""
    if "builder" not in item:
        return str(item["text"])
    module, _, name = str(item["builder"]).partition(":")
    built = getattr(importlib.import_module(module), name)
    return str(built() if callable(built) else built)


def redteam_items() -> list[CorpusUnit]:
    """The items of `redteam.toml`, each with its whole text."""
    return [
        CorpusUnit(
            REDTEAM,
            item["surface"],
            _text_of(item),
            item["expect"],
            item["id"],
            _catchers(REDTEAM, item),
        )
        for item in _items(REDTEAM)
    ]


def both_rules(config: JudgeConfig) -> tuple[JudgeConfig, JudgeConfig]:
    """v0.1.0's way of cutting and counting text, and the configured one."""
    return dataclasses.replace(config, short_text="legacy", unit_overlap_chars=0), config


def redteam_corpus(config: JudgeConfig | None = None) -> list[CorpusUnit]:
    """The red-team items as recorded: a short item as written, a long one as the units each rule
    cuts it into (so the old cut and the overlapping cut both have recordings)."""
    config = config or judge_config()
    units: list[CorpusUnit] = []
    for item in redteam_items():
        if len(item.text) <= config.max_unit_chars:
            units.append(item)
            continue
        pieces = dict.fromkeys(t for rule in both_rules(config) for t in _units(item.text, rule))
        units += [dataclasses.replace(item, text=piece) for piece in pieces]
    return units


def _account_result_text(account_id: str) -> str:
    """What `crm__get_account` returns for an account, as far as an attacker copying values out of
    it is concerned: the account, its contacts in id order and its ten newest notes, as the seed
    data has them and without what the server keeps to itself."""
    from crm_server.seed import build_dataset

    data = build_dataset()
    account = next(a for a in data.accounts if a.id == account_id)
    contacts = sorted((c for c in data.contacts if c.account_id == account_id), key=lambda c: c.id)
    notes = sorted(
        (n for n in data.notes if n.account_id == account_id),
        key=lambda n: n.occurred_at,
        reverse=True,
    )[:10]
    return json.dumps(
        {
            **_row_values(account),
            "contacts": [_row_values(c) for c in contacts],
            "notes": [_row_values(n) for n in notes],
        }
    )


_PLANTED_ID = "TKT-000000"
"""A stand-in for the planted ticket's id: an id is not prose, so the classifier never judges it."""


def attack_strings(config: JudgeConfig | None = None) -> list[CorpusUnit]:
    """The text each attack writes (judged as arguments, and again when a read returns it) and the
    text each attack plants (judged when it is read), under both rules.

    A write that carries what the attack read (`{values:N}`, `{encode:...}`, `{canary}`) is worked
    out by playing the attack's reads against the seed data: the attack copies customer values out
    of the account results it is given, in the order it reads them, so the text it will write is the
    same every run. An argument that cannot be filled this way (the attack never reads the value)
    is left out, as it cannot be known here. A `{pad:N}` is a single word and is never judged."""
    from redteam.attack_format import load_attack
    from redteam.prediction import CONFIG, load_effects
    from redteam.scripted_client import AttackStoppedError, Loot, render

    config = config or judge_config()
    effects = load_effects(CONFIG / "tool_policies.toml", CONFIG / "lab" / "tool_policies.lab.toml")
    units: list[CorpusUnit] = []
    for path in sorted(ATTACKS.glob("*.toml")):
        attack = load_attack(path)
        source = f"attack {attack.id}"
        loot = Loot()
        for step, i in attack.calls():
            if step.tool == "crm__get_account":
                account_id = render(
                    str(step.arguments["account_id"]),
                    attack=attack,
                    step_id=step.id,
                    i=i,
                    planted=_PLANTED_ID,
                    loot=loot,
                )
                loot.note(_account_result_text(account_id))
            if effects.get(step.tool, "write") != "write":
                continue
            rendered: dict[str, Any] = {}
            for key, value in step.arguments.items():
                if not isinstance(value, str):
                    continue
                try:
                    rendered[key] = render(
                        value,
                        attack=attack,
                        step_id=step.id,
                        i=i,
                        planted=_PLANTED_ID,
                        loot=loot,
                    )
                except AttackStoppedError:
                    continue
            units += [
                CorpusUnit(source, surface, text)
                for rule in both_rules(config)
                for text in _units(rendered, rule)
                for surface in _WRITTEN_SURFACES
            ]
        if attack.plant is not None:
            planted = {"subject": attack.plant.subject, "description": attack.plant.text}
            units += [
                CorpusUnit(source, SURFACE_RESULT, text)
                for rule in both_rules(config)
                for text in _units(planted, rule)
            ]
    return units


def everything() -> list[CorpusUnit]:
    return distinct([*must_cover(), *redteam_corpus(), *attack_strings(), *seeded_data()])
