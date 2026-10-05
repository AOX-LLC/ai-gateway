"""The lab-only configuration the scorecard runs against, generated from the attack files.

Nothing here changes production code. The scorecard needs each attack to run as a client of its own
(the egress ledger, the rate limits and the per-client call rate are all per client, so attacks run
as the same client would contaminate each other), while the allowlist rules name their client
exactly. So each attack gets a *clone* of its original client, and every allowlist rule of the
original is copied, with the clone's exact name, into a lab-only allowlist. No pattern matching of
client names exists in the allowlist code or is added to it.

The rate limits and the approval roles need no per-clone entry: a bucket is per client already, and
a role is per tool (a test fails if either format ever gains a client key). The lab gateway reads
the product's two files unchanged (the approval roles plus the lab upstream's write: `config/lab/`).

Also generated: a pipeline file for each column of the scorecard (`pipelines/<column>.toml`), the
three classifier files the "before" columns use (v0.1.0's short-text rule, no unit overlap, or
both), and the list of clients to register. `scripts/generate_lab_config.py` writes them and
`--check` compares; a test does the same. Never use any of it against anything real: it weakens
controls on purpose. Harborline Supply Co. is fictional.
"""

import json
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lab_upstream.tools import definitions as lab_definitions
from redteam.attack_format import load_attack

ROOT = Path(__file__).resolve().parents[2]
ATTACKS = ROOT / "scripts" / "redteam" / "attacks"
LAYER_ORDER = (
    "scope",
    "allowlist",
    "rate_limit",
    "schema",
    "pinned_descriptions",
    "egress",
    "canary",
    "classifier",
    "approval",
)
FLOOR = ("scope", "approval")
LAB_BOT = "harborline-lab-bot"
LAB_BOT_SCOPES = tuple(sorted(f"lab__{tool.name}" for tool in lab_definitions("poisoned")))
"""Every tool the lab upstream can offer in any phase: the lab client may call all of them."""


@dataclass(frozen=True)
class Column:
    id: str
    label: str
    group: str
    modes: Mapping[str, str]
    floor_override: bool = False
    approver: str = "approve"
    """`approve` (the lab approver rubber-stamps every write: the worst case) or `reject`."""
    classifier: str = "current"
    """`current`, or a before variant: `legacy-short-text`, `legacy-overlap`, `legacy-both`."""
    validate_results: bool = True


def _modes(**changes: str) -> dict[str, str]:
    modes = dict.fromkeys(LAYER_ORDER, "enforce")
    modes.update(changes)
    return modes


def _columns() -> list[Column]:
    columns = [
        Column("all-on", "All layers on", "headline", _modes()),
        Column(
            "all-off",
            "All layers off (scope and approval too: lab-only override)",
            "headline",
            dict.fromkeys(LAYER_ORDER, "off"),
            floor_override=True,
        ),
    ]
    for layer in LAYER_ORDER:
        floor = layer in FLOOR
        label = f"Only {layer} off" + (" (lab-only override of a floor layer)" if floor else "")
        columns.append(
            Column(f"off-{layer}", label, "leave-one-out", _modes(**{layer: "off"}), floor)
        )
    columns += [
        Column(
            "monitor",
            "Every layer that may be weakened only watches",
            "monitor",
            _modes(**{layer: "monitor" for layer in LAYER_ORDER if layer not in FLOOR}),
        ),
        Column(
            "before-all-flags",
            "All on, with the three 6a changes turned back to v0.1.0",
            "before-after",
            _modes(),
            classifier="legacy-both",
            validate_results=False,
        ),
        Column(
            "before-short-text",
            "All on, v0.1.0's short-text rule only",
            "before-after",
            _modes(),
            classifier="legacy-short-text",
        ),
        Column(
            "before-overlap",
            "All on, v0.1.0's unit cut (no overlap) only",
            "before-after",
            _modes(),
            classifier="legacy-overlap",
        ),
        Column(
            "before-validate-results",
            "All on, v0.1.0's unchecked results only",
            "before-after",
            _modes(),
            validate_results=False,
        ),
        Column(
            "denying-approver",
            "All layers on, an approver who rejects every write (idealised: assumes a person "
            "always spots the attack)",
            "idealised",
            _modes(),
            approver="reject",
        ),
    ]
    return columns


COLUMNS: tuple[Column, ...] = tuple(_columns())
CLASSIFIER_VARIANTS: dict[str, dict[str, Any]] = {
    "legacy-short-text": {"short_text": "legacy"},
    "legacy-overlap": {"unit_overlap_chars": 0},
    "legacy-both": {"short_text": "legacy", "unit_overlap_chars": 0},
}


@dataclass(frozen=True)
class Clone:
    name: str
    original: str
    attack: str
    scopes: tuple[str, ...]


def clones() -> list[Clone]:
    from ai_gateway.admin.cli import DEMO_CLIENTS

    out = []
    for path in sorted(ATTACKS.glob("*.toml")):
        attack = load_attack(path)
        if attack.client == LAB_BOT:
            scopes = LAB_BOT_SCOPES
        else:
            scopes = tuple(sorted(DEMO_CLIENTS[attack.client][1]))
        out.append(Clone(f"{attack.client}--{attack.id}", attack.client, attack.id, scopes))
    return out


# -- writing the files -----------------------------------------------------------------------------


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    return json.dumps(value, ensure_ascii=False)


def _allowlist_text() -> str:
    product = (ROOT / "config" / "allowlist.toml").read_text(encoding="utf-8")
    entries = tomllib.loads(product).get("rule", [])
    lines = [
        "# GENERATED by scripts/generate_lab_config.py: do not edit. The red-team scorecard's",
        "# allowlist: the product's rules below, unchanged and first, then each rule of an",
        "# original client copied for each of that client's per-attack clones, under the clone's",
        "# exact name. The scorecard runs each attack as a client of its own so that attacks",
        "# cannot contaminate each other's ledger and rate limits. Never used by the product.",
        "# Harborline Supply Co. is fictional.",
        "",
        product.rstrip("\n"),
        "",
    ]
    for clone in clones():
        for entry in entries:
            if entry["client"] != clone.original:
                continue
            copy = {**entry, "name": f"{entry['name']}--{clone.name}", "client": clone.name}
            lines.append("[[rule]]")
            lines += [f"{key} = {_toml_value(value)}" for key, value in copy.items()]
            lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def _pipeline_text(column: Column) -> str:
    lines = [
        "# GENERATED by scripts/generate_lab_config.py: do not edit.",
        "# One column of the red-team scorecard.",
        f"# {column.label}",
    ]
    if column.floor_override:
        lines.append(
            "# LAB-ONLY: a floor layer is weakened under allow_floor_override. Never use this for"
        )
        lines.append("# anything real.")
    lines += ["[layers]"] + [f'{layer} = "{column.modes[layer]}"' for layer in LAYER_ORDER]
    lines += [
        "",
        "[safety]",
        f"allow_floor_override = {_toml_value(column.floor_override)}",
        "",
        "[schema]",
        f"validate_results = {_toml_value(column.validate_results)}",
    ]
    return "\n".join(lines) + "\n"


def _classifier_text(name: str) -> str:
    text = (ROOT / "config" / "classifier.toml").read_text(encoding="utf-8")
    for key, value in CLASSIFIER_VARIANTS[name].items():
        text, count = re.subn(
            rf'^({key} = )(?:"[^"]*"|[0-9]+)', rf"\g<1>{_toml_value(value)}", text, flags=re.M
        )
        assert count == 1, key
    header = (
        "# GENERATED by scripts/generate_lab_config.py: do not edit. The product's classifier\n"
        f"# settings with v0.1.0's behaviour restored ({name}), for the before columns.\n"
    )
    return header + text


def _clients_text() -> str:
    lines = [
        "# GENERATED by scripts/generate_lab_config.py: do not edit. The per-attack clients the",
        "# scorecard registers: each holds exactly the scopes of the original it clones.",
        "# Harborline Supply Co. is fictional.",
        "",
    ]
    for clone in clones():
        lines += [
            "[[client]]",
            f"name = {_toml_value(clone.name)}",
            f"original = {_toml_value(clone.original)}",
            f"attack = {_toml_value(clone.attack)}",
            f"scopes = {json.dumps(list(clone.scopes))}",
            "",
        ]
    return "\n".join(lines).rstrip("\n") + "\n"


def generated_files() -> dict[str, str]:
    """Every generated file, by its path relative to the repository root."""
    files = {
        "config/lab/allowlist.lab.toml": _allowlist_text(),
        "config/lab/clients.lab.toml": _clients_text(),
    }
    for name in CLASSIFIER_VARIANTS:
        files[f"config/lab/classifier.{name}.toml"] = _classifier_text(name)
    for column in COLUMNS:
        files[f"config/lab/pipelines/{column.id}.toml"] = _pipeline_text(column)
    return files
