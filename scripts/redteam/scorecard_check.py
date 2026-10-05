"""The scorecard's static check: no stack needed, so it can run on every pull request.

    uv run scripts/redteam/scorecard_check.py

It fails when the committed `docs/scorecard.json` does not cover exactly the attacks and columns in
the repository, when its predictions are not what the attack files predict, when the Markdown or the
chart is not what the JSON renders to, or when the generated lab configuration is out of date. It
cannot say the *observed* figures are still true: that takes a run (`make scorecard`, or
`scorecard_run.py --check`). Harborline Supply Co. is fictional.
"""

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from redteam import scorecard as sc
from redteam.lab_config import COLUMNS, generated_files
from redteam.prediction import CONFIG, load_effects

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"


def synthetic_card() -> dict[str, Any]:
    """A small scorecard made of invented observations: for the check's own tests."""
    attacks = [
        sc.AttackInfo(
            "bulk", "bulk", "exfil-bulk", "c", "export", 10, True, None, ("egress",), None
        ),
        sc.AttackInfo("benign", "benign", "benign", "c", "none", 0, False, None, (), None),
    ]
    results = {
        column.id: {
            "bulk": sc.Observation(False, False, 2, {"egress": 1}, {}, 0),
            "benign": sc.Observation(False, False, 3, {}, {}, 0),
        }
        for column in COLUMNS
    }
    return sc.build(COLUMNS, attacks, results, {"all-on": [1.0, 2.0], "all-off": [1.0]})


README = ROOT / "README.md"


def problems(
    docs: Path = DOCS, *, compare_to_attack_files: bool = True, readme: Path | None = README
) -> list[str]:
    """Everything the committed scorecard gets wrong, one line each."""
    found: list[str] = []
    path = docs / "scorecard.json"
    if not path.is_file():
        return [f"{path} is missing: run `make scorecard`"]
    card = json.loads(path.read_text(encoding="utf-8"))
    det = card["deterministic"]
    ids = [a["id"] for a in det["attacks"]]
    for column in COLUMNS:
        for attack in ids:
            if attack not in det["results"].get(column.id, {}):
                found.append(f"no observation for column {column.id} and attack {attack}")
    if [c["id"] for c in det["columns"]] != [c.id for c in COLUMNS]:
        found.append("the columns in scorecard.json are not the columns of lab_config.COLUMNS")
    if not found:
        for name, wanted, target in (
            ("scorecard.md", sc.render_markdown(card), docs / "scorecard.md"),
            ("images/scorecard.svg", sc.render_svg(card), docs / "images" / "scorecard.svg"),
        ):
            if not target.is_file() or target.read_text(encoding="utf-8") != wanted:
                found.append(f"{name} is not what scorecard.json renders to: run `make scorecard`")
    if readme is not None and not found:
        found += _readme_problems(card, readme)
    if compare_to_attack_files and not found:
        found += _against_the_attack_files(det)
    return found


def _readme_problems(card: dict[str, Any], readme: Path) -> list[str]:
    text = readme.read_text(encoding="utf-8")
    try:
        wanted = sc.replace_readme_section(text, sc.render_readme_section(card))
    except ValueError:
        return [f"{readme.name} has no scorecard markers ({sc.README_START} ... {sc.README_END})"]
    if wanted != text:
        return [f"the scorecard section of {readme.name} is not what scorecard.json renders to"]
    return []


def _against_the_attack_files(det: dict[str, Any]) -> list[str]:
    from redteam.scorecard_run import attack_info, load_attacks, predict

    found: list[str] = []
    attacks = load_attacks()
    if [a["id"] for a in det["attacks"]] != [a.id for a in attacks]:
        found.append("the attacks in scorecard.json are not the attack files in the repository")
        return found
    effects = load_effects(CONFIG / "tool_policies.toml", CONFIG / "lab" / "tool_policies.lab.toml")
    for committed, attack in zip(det["attacks"], attacks, strict=True):
        info = attack_info(attack)
        for key, value in (
            ("threshold", info.threshold),
            ("gap", info.gap),
            ("family", info.family),
        ):
            if committed[key] != value:
                found.append(
                    f"attack {attack.id}: {key} in scorecard.json is not the attack file's"
                )
        if committed["expected_catchers"] != list(info.expected_catchers):
            found.append(f"attack {attack.id}: the expected catchers are not the attack file's")
        for column in COLUMNS:
            recorded = det["results"][column.id][attack.id]["predicted"]
            if recorded != predict(column, attack, effects):
                found.append(
                    f"attack {attack.id}, column {column.id}: the prediction is not the file's"
                )
    for relative, text in generated_files().items():
        path = ROOT / relative
        if not path.is_file() or path.read_text(encoding="utf-8") != text:
            found.append(f"{relative} is out of date: run scripts/generate_lab_config.py")
    return found


def main() -> None:
    found = problems()
    for line in found:
        print(f"FAIL  {line}", file=sys.stderr)
    if found:
        sys.exit(1)
    print("ok    the committed scorecard, its files and the lab configuration agree")


if __name__ == "__main__":
    main()
