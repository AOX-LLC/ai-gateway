"""Render the one-page case study from its template and the committed scorecard.

    uv run scripts/case_study.py            # or: make case-study
    uv run scripts/case_study.py --check    # or: make case-study-check

`docs/case-study.template.md` is the prose, written by hand. Every figure and every list of attack
or layer names in it is a `{{ placeholder }}`, resolved from `docs/scorecard.json`, so the case
study cannot say something the scorecard does not. `docs/case-study.md` is the rendered result.

`--check` re-renders and compares with the committed file, so a scorecard that changed without a
re-render (or a hand edit of the output) fails the pull request. An unknown placeholder, or a
placeholder this script resolves that the template never uses, is an error: the template and the
script would otherwise drift apart without anyone noticing. Harborline Supply Co. is fictional.
"""

import argparse
import difflib
import json
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
TEMPLATE_NAME = "case-study.template.md"
OUTPUT_NAME = "case-study.md"
SCORECARD_NAME = "scorecard.json"

PLACEHOLDER = re.compile(r"\{\{\s*([a-z0-9_]+)\s*\}\}")
OPENING_BRACES = "{{"
SENTENCE_END = re.compile(r"(?<=[.!?])\s+")

NOTICE = (
    "> Generated from docs/case-study.template.md and docs/scorecard.json by `make case-study`; "
    "do not edit by hand."
)

ALL_ON = "all-on"
ALL_OFF = "all-off"
MONITOR = "monitor"
DENYING_APPROVER = "denying-approver"
BEFORE_AFTER_GROUP = "before-after"

# The scorecard's oracle kinds, grouped by the evidence each one reads. A kind in none of these
# groups is an error, so a new oracle cannot be left out of the case study's count unnoticed.
ORACLE_GROUPS: Mapping[str, tuple[str, ...]] = {
    "oracle_databases": ("export", "canary", "encoded-export"),
    "oracle_ticket_diff": ("unauthorized-write",),
    "oracle_client_observation": ("answered",),
    "oracle_lab_count": ("lab-effect",),
}

# How much of a mismatch's reason the case study quotes: the first sentence says what was predicted.
REASON_SENTENCES_QUOTED = 1
DIFF_LINES_SHOWN = 20
NONE_WORD = "none"


class CaseStudyError(Exception):
    """The template and the scorecard cannot be put together: the message says what to fix."""


# -- Figures from the scorecard -------------------------------------------------------------------


def _names(ids: Sequence[str]) -> str:
    """Attack or layer names as inline code, comma separated; `none` for an empty list."""
    return ", ".join(f"`{name}`" for name in ids) or NONE_WORD


def _percent(rate: float) -> str:
    return f"{rate:.0%}"


def _oracle_counts(hostile: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """How many hostile attacks each kind of evidence judges. Every oracle kind must be grouped."""
    grouped = {kind for kinds in ORACLE_GROUPS.values() for kind in kinds}
    unknown = sorted({a["oracle"] for a in hostile} - grouped)
    if unknown:
        raise CaseStudyError(f"oracle kinds the case study does not count: {', '.join(unknown)}")
    return {
        name: str(sum(1 for a in hostile if a["oracle"] in kinds))
        for name, kinds in ORACLE_GROUPS.items()
    }


def _layer_table(per_layer: Mapping[str, Mapping[str, Any]]) -> str:
    """One row a layer, in the scorecard's order (the gateway's): stopped alone, and redundantly."""
    rows = [
        "| Layer | Stops alone | Also stopped by another layer |",
        "| --- | --- | --- |",
    ]
    for layer, figures in per_layer.items():
        rows.append(
            f"| `{layer}` | {len(figures['contribution'])} | {len(figures['redundant_on'])} |"
        )
    return "\n".join(rows)


def _gap_list(gaps: Sequence[Mapping[str, str]]) -> str:
    return "\n".join(f"- `{g['attack']}`: {g['gap']}." for g in gaps) or f"- {NONE_WORD}"


def _first_sentences(text: str, count: int) -> str:
    return " ".join(SENTENCE_END.split(text.strip())[:count])


def _outcome(succeeded: bool) -> str:
    return "succeeds" if succeeded else "stopped"


def _stopped_by(found: Sequence[Mapping[str, Any]], results: Mapping[str, Any]) -> str:
    """The layers that refused the attack in the columns where it differed, in first-seen order."""
    layers: dict[str, None] = {}
    for mismatch in found:
        observation = results[mismatch["column"]][mismatch["attack"]]
        layers.update(dict.fromkeys(observation["blocked_by"]))
    return f" by {_names(list(layers))}" if layers else ""


def _mismatch_list(
    mismatches: Sequence[Mapping[str, Any]],
    columns: Sequence[Mapping[str, Any]],
    results: Mapping[str, Any],
) -> str:
    """Predicted against observed, one bullet for each attack and direction of difference.

    The before-and-after columns turn old behaviour back on, so a difference there is what they
    are for (the scorecard says so itself). An attack that differs only in those columns is left
    out; one that differs anywhere else is listed, with every column it differed in and the layer
    that stopped it, both read from the data rather than typed here.
    """
    group_of = {c["id"]: c["group"] for c in columns}
    grouped: dict[tuple[str, bool, bool], list[Mapping[str, Any]]] = {}
    for mismatch in mismatches:
        key = (mismatch["attack"], mismatch["predicted"], mismatch["observed"])
        grouped.setdefault(key, []).append(mismatch)
    bullets = []
    for (attack, predicted, observed), found in grouped.items():
        if all(group_of[m["column"]] == BEFORE_AFTER_GROUP for m in found):
            continue
        stopped_by = "" if observed else _stopped_by(found, results)
        reason = _first_sentences(found[0]["reason"], REASON_SENTENCES_QUOTED)
        bullets.append(
            f"- `{attack}`: predicted {_outcome(predicted)}, observed {_outcome(observed)}"
            f"{stopped_by}, in {len(found)} of {len(columns)} columns. {reason}"
        )
    return "\n".join(bullets) or f"- {NONE_WORD}"


def build_context(card: Mapping[str, Any]) -> dict[str, str]:
    """Every placeholder the template may use, as the text that replaces it."""
    det = card["deterministic"]
    summary = det["summary"]
    per_column = summary["per_column"]
    on, off = per_column[ALL_ON], per_column[ALL_OFF]
    monitor, denying = per_column[MONITOR], per_column[DENYING_APPROVER]
    hostile = [a for a in det["attacks"] if a["hostile"]]
    honest_runs = [a for a in det["attacks"] if not a["hostile"]]
    survivors = [a["id"] for a in hostile if det["results"][DENYING_APPROVER][a["id"]]["success"]]
    return {
        "generated_notice": NOTICE,
        "hostile_attacks": str(len(hostile)),
        "honest_runs": str(len(honest_runs)),
        "honest_calls": str(on["benign_calls"]),
        "columns": str(len(det["columns"])),
        **_oracle_counts(hostile),
        "all_on_succeeded": str(on["succeeded"]),
        "all_on_rate": _percent(on["success_rate"]),
        "all_off_succeeded": str(off["succeeded"]),
        "monitor_succeeded": str(monitor["succeeded"]),
        "all_on_honest_blocked": str(on["benign_blocked"]),
        "monitor_honest_would_block": str(monitor["benign_would_block"]),
        "layer_table": _layer_table(summary["per_layer"]),
        "approval_alone": str(len(summary["per_layer"]["approval"]["contribution"])),
        "denying_succeeded": str(denying["succeeded"]),
        "denying_honest_blocked": str(denying["benign_blocked"]),
        "denying_honest_rate": _percent(denying["false_positive_rate"]),
        "denying_survivors": _names(survivors),
        "gap_count": str(len(summary["gaps"])),
        "gap_list": _gap_list(summary["gaps"]),
        "undeclared_successes": str(len(summary["undeclared_successes"])),
        "mismatch_list": _mismatch_list(summary["mismatches"], det["columns"], det["results"]),
        "unclassified_total": str(sum(c["unclassified"] for c in per_column.values())),
    }


# -- Rendering ------------------------------------------------------------------------------------


def render(template: str, context: Mapping[str, str]) -> str:
    """Fill the template. An unknown or an unused placeholder raises `CaseStudyError`."""
    found = PLACEHOLDER.findall(template)
    if template.count(OPENING_BRACES) != len(found):
        raise CaseStudyError(
            f"a `{OPENING_BRACES}` in the template is not a placeholder: names are lowercase "
            "letters, digits and underscores"
        )
    unknown = sorted(set(found) - set(context))
    if unknown:
        raise CaseStudyError(f"unknown placeholder in the template: {', '.join(unknown)}")
    unused = sorted(set(context) - set(found))
    if unused:
        raise CaseStudyError(f"placeholder the template never uses: {', '.join(unused)}")
    return PLACEHOLDER.sub(lambda match: context[match.group(1)], template)


def render_from(docs: Path) -> str:
    """The case study as `docs/` says it should read: its template with its scorecard's figures."""
    card = json.loads((docs / SCORECARD_NAME).read_text(encoding="utf-8"))
    template = (docs / TEMPLATE_NAME).read_text(encoding="utf-8")
    return render(template, build_context(card))


def problems(docs: Path = DOCS) -> list[str]:
    """What is wrong with the committed case study, one message each; empty when it is current."""
    try:
        wanted = render_from(docs)
    except (CaseStudyError, OSError, KeyError, ValueError) as error:
        return [f"the case study cannot be rendered: {error}"]
    target = docs / OUTPUT_NAME
    if not target.is_file():
        return [f"docs/{OUTPUT_NAME} is missing: run `make case-study`"]
    committed = target.read_text(encoding="utf-8")
    if committed == wanted:
        return []
    diff = difflib.unified_diff(
        committed.splitlines(),
        wanted.splitlines(),
        fromfile=f"docs/{OUTPUT_NAME} (committed)",
        tofile=f"docs/{OUTPUT_NAME} (rendered)",
        lineterm="",
    )
    shown = list(diff)[:DIFF_LINES_SHOWN]
    return [
        f"docs/{OUTPUT_NAME} is not what {TEMPLATE_NAME} and {SCORECARD_NAME} render to: "
        "run `make case-study`",
        *shown,
    ]


def write(docs: Path = DOCS) -> None:
    (docs / OUTPUT_NAME).write_text(render_from(docs), encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="compare, do not write")
    parser.add_argument("--docs", type=Path, default=DOCS, help="the docs directory (for tests)")
    args = parser.parse_args(argv)
    if args.check:
        found = problems(args.docs)
        for line in found:
            print(f"FAIL  {line}", file=sys.stderr)
        if found:
            return 1
        print("ok    docs/case-study.md is what its template and docs/scorecard.json render to")
        return 0
    try:
        write(args.docs)
    except CaseStudyError as error:
        print(f"FAIL  {error}", file=sys.stderr)
        return 1
    print(f"rendered docs/{OUTPUT_NAME}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
