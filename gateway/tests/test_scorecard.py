"""The scorecard's arithmetic and its renderings, from observations made up for the purpose.

The runner (`scripts/redteam/scorecard_run.py`) turns a live gateway into `Observation`s; everything
after that is pure and tested here: the per-column and per-layer figures, what each layer
contributes, the false-positive rate, predicted against observed, the before and after of the three
6a flags, the Markdown, and the chart (its colours must be the portfolio UI's, and it must carry its
numbers as text). Harborline Supply Co. is fictional.
"""

import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from redteam import scorecard as sc  # noqa: E402
from redteam.lab_config import COLUMNS  # noqa: E402

COLUMN_IDS = [c.id for c in COLUMNS]


def attack(
    id: str,
    family: str = "exfil-bulk",
    *,
    hostile: bool = True,
    gap: str | None = None,
    catchers: tuple[str, ...] = ("egress",),
) -> sc.AttackInfo:
    return sc.AttackInfo(
        id=id,
        title=id,
        family=family,
        client="c",
        oracle="export",
        threshold=10,
        hostile=hostile,
        gap=gap,
        expected_catchers=catchers,
        lab_phase=None,
    )


def obs(
    success: bool,
    predicted: bool = False,
    blocked: dict[str, int] | None = None,
    would: dict[str, int] | None = None,
    calls: int = 3,
    unclassified: int = 0,
) -> sc.Observation:
    return sc.Observation(
        success=success,
        predicted=predicted,
        calls=calls,
        blocked_by=blocked or {},
        would_block=would or {},
        unclassified=unclassified,
    )


ATTACKS = [
    attack("bulk", catchers=("egress",)),
    attack("canary", "canary", catchers=("canary",)),
    attack("drip", "exfil-drip", gap="egress lets nine values out", catchers=()),
    attack("benign", "benign", hostile=False, catchers=()),
]


def results(**overrides: dict[str, sc.Observation]) -> dict[str, dict[str, sc.Observation]]:
    base = {
        "bulk": obs(False, blocked={"egress": 1}),
        "canary": obs(False, blocked={"canary": 1}),
        "drip": obs(True, predicted=True),
        "benign": obs(False, calls=5),
    }
    out = {column: dict(base) for column in COLUMN_IDS}
    out["all-off"] = {
        "bulk": obs(True),
        "canary": obs(True),
        "drip": obs(True),
        "benign": obs(False, calls=5),
    }
    out["off-egress"]["bulk"] = obs(True)
    out["off-canary"]["canary"] = obs(True)
    out["monitor"] = {
        "bulk": obs(True, would={"egress": 1}),
        "canary": obs(True, would={"canary": 1}),
        "drip": obs(True),
        "benign": obs(False, calls=5, would={"classifier": 1}),
    }
    for column, changes in overrides.items():
        out[column].update(changes)
    return out


def card(**overrides: dict[str, sc.Observation]) -> dict[str, Any]:
    return sc.build(
        COLUMNS,
        ATTACKS,
        results(**overrides),
        latency={"all-on": [10.0, 12.0, 30.0], "all-off": [8.0, 9.0, 11.0]},
    )


# -- the figures ----------------------------------------------------------------------------------


def test_a_column_counts_how_many_hostile_attacks_succeeded_and_ignores_the_benign_run() -> None:
    columns = card()["deterministic"]["summary"]["per_column"]

    assert columns["all-on"]["hostile"] == 3
    assert columns["all-on"]["succeeded"] == 1, "only the drip, a recorded gap"
    assert columns["all-off"]["succeeded"] == 3
    assert columns["all-on"]["success_rate"] == pytest.approx(1 / 3)
    assert columns["monitor"]["succeeded"] == 3


def test_a_layer_contributes_the_attacks_that_succeed_only_when_it_is_off() -> None:
    layers = card()["deterministic"]["summary"]["per_layer"]

    assert layers["egress"]["contribution"] == ["bulk"]
    assert layers["canary"]["contribution"] == ["canary"]
    assert layers["scope"]["contribution"] == []
    assert layers["egress"]["catches_in_all_on"] == ["bulk"]


def test_a_layer_whose_attack_is_also_stopped_by_another_is_redundant_not_idle() -> None:
    layers = card(**{"off-egress": {"bulk": obs(False, blocked={"canary": 1})}})["deterministic"][
        "summary"
    ]["per_layer"]

    assert layers["egress"]["contribution"] == []
    assert layers["egress"]["redundant_on"] == ["bulk"], "caught by egress, and stopped without it"


def test_false_positives_are_benign_calls_a_layer_blocked_or_would_have_blocked() -> None:
    results_ = results(**{"all-on": {"benign": obs(False, calls=5, blocked={"classifier": 1})}})
    summary = sc.build(COLUMNS, ATTACKS, results_)["deterministic"]["summary"]

    assert summary["per_column"]["all-on"]["benign_calls"] == 5
    assert summary["per_column"]["all-on"]["benign_blocked"] == 1
    assert summary["per_column"]["all-on"]["false_positive_rate"] == pytest.approx(0.2)
    assert summary["per_layer"]["classifier"]["false_positives_all_on"] == 1
    assert summary["per_layer"]["classifier"]["would_block_benign_monitor"] == 1


def test_unclassified_calls_are_counted_apart_from_clean_ones() -> None:
    results_ = results(**{"all-on": {"drip": obs(True, predicted=True, unclassified=2)}})

    summary = sc.build(COLUMNS, ATTACKS, results_)["deterministic"]["summary"]

    assert summary["per_column"]["all-on"]["unclassified"] == 2


def test_the_gaps_are_the_attacks_that_succeed_with_every_layer_on_and_say_so() -> None:
    summary = card()["deterministic"]["summary"]

    assert summary["gaps"] == [{"attack": "drip", "gap": "egress lets nine values out"}]


def test_predicted_against_observed_lists_every_difference_without_hiding_any() -> None:
    mismatched = card(**{"all-on": {"bulk": obs(True, predicted=False)}})["deterministic"][
        "summary"
    ]

    assert {
        "column": "all-on",
        "attack": "bulk",
        "predicted": False,
        "observed": True,
        "reason": None,
    } in mismatched["mismatches"]
    assert card()["deterministic"]["summary"]["per_column"]["all-on"]["mismatches"] == 0


def test_before_and_after_shows_each_attack_a_flag_changed() -> None:
    results_ = results(**{"before-short-text": {"bulk": obs(True)}})

    flag = sc.build(COLUMNS, ATTACKS, results_)["deterministic"]["summary"]["before_after"][
        "before-short-text"
    ]

    assert flag["changed"] == [{"attack": "bulk", "before": True, "after": False}]
    assert card()["deterministic"]["summary"]["before_after"]["before-overlap"]["changed"] == []


def test_the_two_long_boundary_attacks_are_reported_side_by_side_when_both_are_present() -> None:
    attacks = [
        *ATTACKS,
        attack("obfuscated-long-boundary", "obfuscated-instruction", catchers=("classifier",)),
        attack(
            "obfuscated-long-boundary-split", "obfuscated-instruction", catchers=("classifier",)
        ),
    ]
    results_ = results()
    for column in COLUMN_IDS:
        results_[column]["obfuscated-long-boundary"] = obs(False, blocked={"classifier": 1})
        results_[column]["obfuscated-long-boundary-split"] = obs(False, blocked={"classifier": 1})
    results_["before-overlap"]["obfuscated-long-boundary"] = obs(False, blocked={"classifier": 1})
    results_["before-overlap"]["obfuscated-long-boundary-split"] = obs(True)

    both = sc.build(COLUMNS, attacks, results_)["deterministic"]["summary"]["long_boundary"]

    assert both["obfuscated-long-boundary"] == {
        "all-on": False,
        "before-overlap": False,
        "overlap_makes_a_difference": False,
    }
    assert both["obfuscated-long-boundary-split"] == {
        "all-on": False,
        "before-overlap": True,
        "overlap_makes_a_difference": True,
    }


def test_latency_is_indicative_and_apart_from_what_a_check_compares() -> None:
    built = card()

    assert set(built) == {"schema_version", "deterministic", "indicative"}
    latency = built["indicative"]["latency_ms"]
    assert latency["all-on"]["n"] == 3
    assert latency["all-on"]["p50"] == pytest.approx(12.0)
    assert built["indicative"]["overhead_p50_ms"] == pytest.approx(3.0)
    assert "latency" not in json.dumps(built["deterministic"]).lower()


def test_the_deterministic_part_does_not_change_with_the_latency_samples() -> None:
    a = sc.build(COLUMNS, ATTACKS, results(), latency={"all-on": [1.0]})
    b = sc.build(COLUMNS, ATTACKS, results(), latency={"all-on": [999.0]})

    assert a["deterministic"] == b["deterministic"]
    assert a["indicative"] != b["indicative"]


def test_a_missing_observation_is_an_error_never_a_quiet_zero() -> None:
    incomplete = results()
    del incomplete["all-on"]["bulk"]

    with pytest.raises(ValueError, match=r"all-on.*bulk"):
        sc.build(COLUMNS, ATTACKS, incomplete)


# -- the rendering --------------------------------------------------------------------------------


def test_the_markdown_carries_every_honesty_note_and_labels_the_floor_overrides() -> None:
    text = sc.render_markdown(card())

    for phrase in (
        "compliant model",
        "replay",
        "known gaps",
        "lab-only",
        "small corpus",
        "both long-boundary",
        "rubber-stamp",
        "fictional",
    ):
        assert phrase.lower() in text.lower(), phrase
    assert "| drip |" in text or "drip" in text
    assert "egress lets nine values out" in text
    assert "make scorecard" in text


def test_the_markdown_renders_from_the_json_alone() -> None:
    built = card()

    assert sc.render_markdown(json.loads(json.dumps(built))) == sc.render_markdown(built)


# -- the chart ------------------------------------------------------------------------------------

CSS = (ROOT / "dashboard" / "src" / "styles" / "portfolio-ui.css").read_text(encoding="utf-8")


def test_the_chart_is_valid_svg_with_its_numbers_as_text_and_a_title_and_description() -> None:
    svg = sc.render_svg(card())

    root = ET.fromstring(svg)  # noqa: S314 - the chart is generated here
    assert root.tag.endswith("svg")
    assert root.find("{http://www.w3.org/2000/svg}title") is not None
    assert root.find("{http://www.w3.org/2000/svg}desc") is not None
    texts = [t.text or "" for t in root.iter("{http://www.w3.org/2000/svg}text")]
    assert any("1 of 3" in t for t in texts), "all on: one of three hostile attacks succeeds"
    assert any("3 of 3" in t for t in texts), "all off"
    assert any("fictional" in t.lower() for t in texts)
    assert any("worst-case approver" in t.lower() for t in texts)


def test_every_colour_in_the_chart_is_a_portfolio_ui_token_in_both_themes() -> None:
    svg = sc.render_svg(card())
    used = {c.upper() for c in re.findall(r"#[0-9A-Fa-f]{6}\b", svg)}
    tokens = {c.upper() for c in re.findall(r"#[0-9A-Fa-f]{6}\b", CSS)}

    assert used, "the chart uses colour"
    assert used <= tokens, sorted(used - tokens)
    assert "prefers-color-scheme: dark" in svg


def test_the_chart_does_not_depend_on_colour_alone() -> None:
    svg = sc.render_svg(card())

    assert svg.count("<text") >= 2 * len(COLUMN_IDS) // 2
    assert "lab-only" in svg.lower()


def test_the_chart_is_the_same_every_time() -> None:
    assert sc.render_svg(card()) == sc.render_svg(card())


def test_an_attack_that_could_not_finish_is_listed_and_not_hidden_in_the_figures() -> None:
    cut_short = sc.Observation(
        success=False,
        predicted=True,
        calls=2,
        blocked_by={"rate_limit": 1},
        would_block={},
        unclassified=0,
        incomplete=True,
    )
    built = card(**{"off-egress": {"bulk": cut_short}})

    summary = built["deterministic"]["summary"]
    assert summary["incomplete"] == [{"column": "off-egress", "attack": "bulk"}]
    assert built["deterministic"]["results"]["off-egress"]["bulk"]["incomplete"] is True
    assert card()["deterministic"]["summary"]["incomplete"] == []
    assert "could not finish" in sc.render_markdown(built).lower()


# -- the prose says only what the data and the code say -------------------------------------------


def test_the_floor_override_is_described_as_the_product_accepts_it() -> None:
    text = sc.render_markdown(card())

    assert "the product refuses that configuration" not in text
    assert "LAB_FLOOR_OVERRIDE=yes" in text
    assert "allow_floor_override" in text


def test_the_oracle_is_described_by_the_kinds_of_evidence_the_attacks_actually_use() -> None:
    text = sc.render_markdown(card())

    assert "independent oracle that reads the databases and shares" not in text, "not true of every"
    assert "3 judged by what landed in the databases" in text


def test_the_known_gaps_paragraph_lists_the_gaps_in_the_data_and_none_that_are_not() -> None:
    with_gap = sc.render_markdown(card())
    none = sc.render_markdown(card(**{"all-on": {"drip": obs(False, predicted=True)}}))

    section = with_gap.split("## What this does and does not show")[1]
    assert "`drip`" in section
    assert "ROT13" not in section, "a gap is named only if it is in the data"
    other = none.split("## What this does and does not show")[1]
    assert "`drip`" not in other
    assert "no attack succeeds with every layer on" in other.lower()


def test_the_long_boundary_paragraph_reports_what_was_observed_not_what_was_expected() -> None:
    attacks = [
        *ATTACKS,
        attack("obfuscated-long-boundary", "obfuscated-instruction"),
        attack("obfuscated-long-boundary-split", "obfuscated-instruction"),
    ]
    results_ = results()
    for column in COLUMN_IDS:
        results_[column]["obfuscated-long-boundary"] = obs(False)
        results_[column]["obfuscated-long-boundary-split"] = obs(False)
    results_["before-overlap"]["obfuscated-long-boundary-split"] = obs(True)

    text = sc.render_markdown(sc.build(COLUMNS, attacks, results_))

    part = text.split("## Both long-boundary attacks")[1].split("## ")[0]
    assert "flagged both fragments" not in part
    first = "`obfuscated-long-boundary`: stopped with overlap, stopped without it: "
    second = "`obfuscated-long-boundary-split`: stopped with overlap, succeeds without it: "
    assert first + "overlap makes no difference" in part
    assert second + "overlap makes a difference" in part


# -- the README's section is rendered from the same JSON ------------------------------------------


def test_the_readme_section_states_the_headline_from_the_data_and_carries_the_honesty_line() -> (
    None
):
    section = sc.render_readme_section(card())

    assert section.startswith("<!-- scorecard:start -->")
    assert section.rstrip().endswith("<!-- scorecard:end -->")
    assert "docs/images/scorecard.svg" in section
    assert "1 of 3" in section, "with every layer on (the recorded gap)"
    assert "3 of 3" in section, "with every layer off"
    assert "`drip`" in section, "the gap is named"
    assert "compliant" in section.lower()
    assert "docs/scorecard.md" in section
    assert "make scorecard" in section


def test_the_readme_section_lists_what_each_layer_alone_stops() -> None:
    section = sc.render_readme_section(card())

    assert "`egress`: 1" in section
    assert "`canary`: 1" in section
    assert "`scope`: 0" in section


def test_the_section_is_replaced_in_place_and_nothing_else_in_the_readme_changes() -> None:
    readme = (
        "# Title\n\nintro\n\n<!-- scorecard:start -->\nold\n<!-- scorecard:end -->\n\n## Licence\n"
    )

    updated = sc.replace_readme_section(readme, sc.render_readme_section(card()))

    assert updated.startswith("# Title\n\nintro\n\n")
    assert updated.endswith("\n\n## Licence\n")
    assert "old" not in updated
    assert updated.count("<!-- scorecard:start -->") == 1
    with pytest.raises(ValueError, match="markers"):
        sc.replace_readme_section("# no markers here\n", "x")


def test_the_axis_labels_sit_clear_of_the_subtitle() -> None:
    root = ET.fromstring(sc.render_svg(card()))  # noqa: S314 - the chart is generated here
    ns = "{http://www.w3.org/2000/svg}"
    texts = list(root.iter(f"{ns}text"))
    subtitle_bottom = max(
        float(t.get("y", 0))
        for t in texts
        if "Harborline Supply Co. is fictional" in (t.text or "")
    )
    ticks = [
        float(t.get("y", 0))
        for t in texts
        if (t.text or "").isdigit() and t.get("text-anchor") == "middle"
    ]

    assert ticks, "the axis has labels"
    assert min(ticks) - 11 > subtitle_bottom, "a tick label (11px high) must not touch the subtitle"


def test_the_denying_approver_columns_false_positives_are_explained_not_left_to_be_misread() -> (
    None
):
    text = sc.render_markdown(card())

    assert "rejects every write, honest ones included" in text


def test_a_difference_with_no_written_reason_says_so_and_one_with_a_reason_carries_it() -> None:
    from redteam.explanations import REASONS
    from redteam.scorecard import render_markdown

    (attack, column), reason = next(iter(REASONS.items()))
    unexplained = card(**{"all-on": {"bulk": obs(True, predicted=False)}})

    assert "not yet explained" in render_markdown(unexplained)
    assert attack == "smuggle-oversized-argument"
    assert column in {"off-schema", "monitor"}
    assert "65 536 bytes" in reason
