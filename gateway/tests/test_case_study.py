"""The case study is the scorecard's figures in prose, and it cannot drift from them.

`docs/case-study.template.md` is hand-written and holds no number: every figure and every list of
attack or layer names is a placeholder that `scripts/case_study.py` fills from
`docs/scorecard.json`. These tests pin that: the committed `docs/case-study.md` is what the
script renders, an unknown or unused placeholder fails, a figure that changed without a re-render
is caught by `--check`, and the template types no number of its own. Harborline Supply Co. is
fictional."""

import json
import re
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import case_study  # noqa: E402
from ai_gateway.pipeline.registry import LAYER_ORDER  # noqa: E402

DOCS = ROOT / "docs"
TEMPLATE = DOCS / case_study.TEMPLATE_NAME

# What the template may spell with a digit, because it is not a figure from the scorecard: a date,
# a version, one of the project's own ports, and two names that happen to contain digits.
ALLOWED_DIGIT_PATTERNS = (
    r"\b\d{4}-\d{2}-\d{2}\b",
    r"\bv?\d+\.\d+(?:\.\d+)?\b",
    r"\b44(?:0\d|1[0-3])\b",
    r"\bROT13\b",
    r"\bbase64\b",
)
# Small counts written as words are numbers too. The few phrases below use one as a pronoun or as
# the name of a result, not as a count of the scorecard's things.
NUMBER_WORDS = (
    "zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|"
    "fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|hundred|thousand|"
    "dozen|half|once|twice|single|both|pair|couple"
)
ALLOWED_PHRASES = (
    "A zero means",
    "a compliant model, one already talked into it",
    "a recorded judgement of one string",
)
PLACEHOLDER_SPAN = re.compile(r"\{\{[^}]*\}\}")
LAYER_BULLET = re.compile(r"^- `([a-z_]+)`:", re.MULTILINE)


def hard_coded_numbers(text: str) -> list[str]:
    """Digits and number words in prose, outside placeholders and the allow-list above."""
    prose = PLACEHOLDER_SPAN.sub("", text)
    for phrase in ALLOWED_PHRASES:
        prose = prose.replace(phrase, "")
    for pattern in ALLOWED_DIGIT_PATTERNS:
        prose = re.sub(pattern, "", prose)
    prose = re.sub(r"\]\([^)]*\)", "]", prose)  # a link's target is not prose
    return re.findall(rf"\d+|\b(?:{NUMBER_WORDS})\b", prose, flags=re.IGNORECASE)


@pytest.fixture
def docs_copy(tmp_path: Path) -> Path:
    """A private copy of the files `--check` reads, to change without touching the repository."""
    for name in (case_study.TEMPLATE_NAME, case_study.OUTPUT_NAME, case_study.SCORECARD_NAME):
        shutil.copy(DOCS / name, tmp_path / name)
    return tmp_path


def test_the_committed_case_study_is_what_the_template_and_the_scorecard_render_to() -> None:
    assert case_study.problems(DOCS) == []
    assert (DOCS / case_study.OUTPUT_NAME).read_text(encoding="utf-8") == case_study.render_from(
        DOCS
    )


def test_rendering_is_deterministic_and_writing_it_produces_the_committed_file(
    docs_copy: Path,
) -> None:
    assert case_study.render_from(docs_copy) == case_study.render_from(docs_copy)
    (docs_copy / case_study.OUTPUT_NAME).unlink()

    assert case_study.main(["--docs", str(docs_copy)]) == 0

    assert (docs_copy / case_study.OUTPUT_NAME).read_bytes() == (
        DOCS / case_study.OUTPUT_NAME
    ).read_bytes()


def test_the_rendered_case_study_says_it_is_generated_and_that_harborline_is_fictional() -> None:
    top = (DOCS / case_study.OUTPUT_NAME).read_text(encoding="utf-8").splitlines()[:6]

    assert any(
        "Generated from docs/case-study.template.md and docs/scorecard.json by `make case-study`; "
        "do not edit by hand." in line
        for line in top
    )
    assert any("Harborline Supply Co." in line and "fictional" in line for line in top)


def test_no_placeholder_is_left_in_the_rendered_case_study() -> None:
    assert "{{" not in (DOCS / case_study.OUTPUT_NAME).read_text(encoding="utf-8")


def test_an_unknown_placeholder_fails_loudly() -> None:
    card = json.loads((DOCS / case_study.SCORECARD_NAME).read_text(encoding="utf-8"))
    context = case_study.build_context(card)
    template = "{{ generated_notice }}\n{{ not_a_figure }}\n" + "\n".join(
        f"{{{{ {name} }}}}" for name in context if name != "generated_notice"
    )

    with pytest.raises(case_study.CaseStudyError, match="not_a_figure"):
        case_study.render(template, context)


def test_a_placeholder_the_template_never_uses_fails_loudly() -> None:
    card = json.loads((DOCS / case_study.SCORECARD_NAME).read_text(encoding="utf-8"))
    context = case_study.build_context(card)

    with pytest.raises(case_study.CaseStudyError, match="never uses"):
        case_study.render("{{ generated_notice }}\n", context)


def test_braces_that_are_not_a_placeholder_fail_loudly() -> None:
    with pytest.raises(case_study.CaseStudyError, match="not a placeholder"):
        case_study.render("{{ Not A Name }}", {})


def test_a_missing_template_or_scorecard_is_reported_not_raised(tmp_path: Path) -> None:
    found = case_study.problems(tmp_path)

    assert len(found) == 1
    assert "cannot be rendered" in found[0]


def test_a_changed_figure_that_was_not_re_rendered_is_caught_by_check(
    docs_copy: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert case_study.main(["--check", "--docs", str(docs_copy)]) == 0
    path = docs_copy / case_study.SCORECARD_NAME
    card = json.loads(path.read_text(encoding="utf-8"))
    card["deterministic"]["summary"]["per_column"]["all-on"]["succeeded"] += 1
    path.write_text(json.dumps(card, indent=2) + "\n", encoding="utf-8")

    assert case_study.main(["--check", "--docs", str(docs_copy)]) == 1

    error = capsys.readouterr().err
    assert "make case-study" in error
    assert "case-study.md" in error
    assert case_study.main(["--docs", str(docs_copy)]) == 0
    assert case_study.main(["--check", "--docs", str(docs_copy)]) == 0


def test_a_hand_edit_of_the_rendered_file_is_caught_by_check(
    docs_copy: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = docs_copy / case_study.OUTPUT_NAME
    target.write_text(target.read_text(encoding="utf-8") + "\nedited by hand\n", encoding="utf-8")

    assert case_study.main(["--check", "--docs", str(docs_copy)]) == 1

    assert "edited by hand" in capsys.readouterr().err


def test_a_missing_rendered_file_is_caught_by_check(docs_copy: Path) -> None:
    (docs_copy / case_study.OUTPUT_NAME).unlink()

    assert any("missing" in line for line in case_study.problems(docs_copy))


def test_a_changed_gap_or_a_changed_denying_column_reaches_the_rendered_text(
    docs_copy: Path,
) -> None:
    path = docs_copy / case_study.SCORECARD_NAME
    card = json.loads(path.read_text(encoding="utf-8"))
    summary = card["deterministic"]["summary"]
    summary["gaps"][0]["gap"] = "a gap sentence that only this test writes"
    summary["per_column"]["denying-approver"]["benign_blocked"] = 7

    rendered = case_study.render(
        (docs_copy / case_study.TEMPLATE_NAME).read_text(encoding="utf-8"),
        case_study.build_context(card),
    )

    assert "a gap sentence that only this test writes" in rendered
    assert "7 of 19 honest calls" in rendered


def test_the_template_hard_codes_no_number() -> None:
    template = TEMPLATE.read_text(encoding="utf-8")

    assert hard_coded_numbers(template) == []


@pytest.mark.parametrize(
    ("prose", "expected"),
    [
        ("There are 31 attacks.", ["31"]),
        ("Five attacks still succeed.", ["Five"]),
        ("In 16% of runs.", ["16"]),
        ("It takes a single run.", ["single"]),
        ("Port 4401 is allowed but not 9999.", ["9999"]),
    ],
)
def test_the_hard_coded_number_scan_does_catch_a_number(prose: str, expected: list[str]) -> None:
    assert hard_coded_numbers(prose) == expected


def test_the_hard_coded_number_scan_leaves_placeholders_dates_and_versions_alone() -> None:
    assert hard_coded_numbers("{{ hostile_attacks }} on 2026-10-05 under v0.2.0, ROT13.") == []


def test_the_template_has_no_em_dash_and_labels_harborline_as_fictional() -> None:
    template = TEMPLATE.read_text(encoding="utf-8")

    assert "—" not in template
    assert re.search(r"Harborline Supply Co\. and all data here are fictional", template)


def test_the_designs_layer_list_is_in_the_gateways_own_order() -> None:
    template = TEMPLATE.read_text(encoding="utf-8")
    card = json.loads((DOCS / case_study.SCORECARD_NAME).read_text(encoding="utf-8"))

    in_template = LAYER_BULLET.findall(template)
    in_gateway = [layer.name for layer in LAYER_ORDER]
    in_scorecard = list(card["deterministic"]["summary"]["per_layer"])

    assert in_template == in_gateway
    assert in_scorecard == in_gateway


def test_the_makefile_and_the_ci_job_run_the_case_study_check() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    job = workflow.split("  scorecard-static:")[1].split("\n  scorecard:")[0]

    assert "case-study:\n\tuv run scripts/case_study.py\n" in makefile
    assert "case-study-check:\n\tuv run scripts/case_study.py --check\n" in makefile
    phony = next(line for line in makefile.splitlines() if line.startswith(".PHONY:"))
    assert "case-study" in phony.split()
    assert "case-study-check" in phony.split()
    assert "- run: make case-study-check" in job
