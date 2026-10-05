"""The static check that runs on every pull request: the committed scorecard, its rendered files and
the generated lab configuration agree with the attack files and with each other, with no stack.

A full regeneration (`make scorecard`, or `scorecard_run.py --check`) is the other half: it needs a
stack, and runs in the end-to-end job or on main and nightly. Harborline Supply Co. is fictional."""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from redteam import scorecard as sc  # noqa: E402
from redteam import scorecard_check as check  # noqa: E402

DOCS = ROOT / "docs"


def test_the_committed_scorecard_files_exist_and_say_so_when_they_do_not(tmp_path: Path) -> None:
    problems = check.problems(tmp_path)

    assert any("scorecard.json" in p for p in problems), problems


def test_a_scorecard_made_from_synthetic_observations_passes_the_check_when_it_matches(
    tmp_path: Path,
) -> None:
    card = check.synthetic_card()
    (tmp_path / "images").mkdir()
    (tmp_path / "scorecard.json").write_text(json.dumps(card, indent=2) + "\n", encoding="utf-8")
    (tmp_path / "scorecard.md").write_text(sc.render_markdown(card), encoding="utf-8")
    (tmp_path / "images" / "scorecard.svg").write_text(sc.render_svg(card), encoding="utf-8")

    assert check.problems(tmp_path, compare_to_attack_files=False, readme=None) == []


@pytest.mark.parametrize("which", ["md", "svg"])
def test_a_rendered_file_that_is_not_what_the_json_renders_to_is_caught(
    tmp_path: Path, which: str
) -> None:
    card = check.synthetic_card()
    (tmp_path / "images").mkdir()
    (tmp_path / "scorecard.json").write_text(json.dumps(card, indent=2) + "\n", encoding="utf-8")
    (tmp_path / "scorecard.md").write_text(sc.render_markdown(card), encoding="utf-8")
    (tmp_path / "images" / "scorecard.svg").write_text(sc.render_svg(card), encoding="utf-8")
    target = tmp_path / ("scorecard.md" if which == "md" else "images/scorecard.svg")
    target.write_text(
        target.read_text(encoding="utf-8") + "\n<!-- edited by hand -->\n", encoding="utf-8"
    )

    problems = check.problems(tmp_path, compare_to_attack_files=False, readme=None)

    assert any("render" in p for p in problems), problems


def test_a_scorecard_that_does_not_cover_every_attack_and_column_is_caught(tmp_path: Path) -> None:
    card = check.synthetic_card()
    del card["deterministic"]["results"]["all-on"]["bulk"]
    (tmp_path / "images").mkdir()
    (tmp_path / "scorecard.json").write_text(json.dumps(card, indent=2) + "\n", encoding="utf-8")
    (tmp_path / "scorecard.md").write_text("x", encoding="utf-8")
    (tmp_path / "images" / "scorecard.svg").write_text("x", encoding="utf-8")

    problems = check.problems(tmp_path, compare_to_attack_files=False, readme=None)

    assert any("all-on" in p and "bulk" in p for p in problems), problems


def test_the_committed_scorecard_agrees_with_the_attack_files_and_the_lab_config() -> None:
    """Skipped until the scorecard has been generated once (`make scorecard`)."""
    if not (DOCS / "scorecard.json").is_file():
        pytest.skip("docs/scorecard.json has not been generated yet")

    assert check.problems(DOCS) == []


def test_the_makefile_has_the_targets_the_documentation_names() -> None:
    text = (ROOT / "Makefile").read_text(encoding="utf-8")

    for target in ("scorecard:", "scorecard-check:", "scorecard-verify:", "lab-config:"):
        assert target in text
    assert "scorecard_run.py" in text
    assert "AGENT_CORE_ANTHROPIC_API_KEY" not in text, "replay mode needs no key"


def test_the_check_compares_the_readme_section_with_what_the_json_renders_to(
    tmp_path: Path,
) -> None:
    card = check.synthetic_card()
    (tmp_path / "images").mkdir()
    (tmp_path / "scorecard.json").write_text(json.dumps(card, indent=2) + "\n", encoding="utf-8")
    (tmp_path / "scorecard.md").write_text(sc.render_markdown(card), encoding="utf-8")
    (tmp_path / "images" / "scorecard.svg").write_text(sc.render_svg(card), encoding="utf-8")
    readme = tmp_path / "README.md"
    readme.write_text(
        sc.replace_readme_section(
            "# T\n\n" + sc.README_START + "\nold\n" + sc.README_END + "\n",
            sc.render_readme_section(card),
        ),
        encoding="utf-8",
    )

    assert check.problems(tmp_path, compare_to_attack_files=False, readme=readme) == []
    readme.write_text(
        readme.read_text(encoding="utf-8").replace("make scorecard", "edited"), encoding="utf-8"
    )
    assert any(
        "README" in p
        for p in check.problems(tmp_path, compare_to_attack_files=False, readme=readme)
    )
    readme.write_text("# no markers\n", encoding="utf-8")
    assert any(
        "markers" in p
        for p in check.problems(tmp_path, compare_to_attack_files=False, readme=readme)
    )
