"""The corpus is well formed: every item names the layers that should catch it.

The rule, in `config/classifier_corpus/README.md` and written before it was applied: the classifier
is an expected catcher of an item only if its text contains an instruction directed at a model.
Harborline Supply Co. is fictional.
"""

import dataclasses
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import classifier_corpus as corpus  # noqa: E402
from ai_gateway.classifier.judge import JudgeConfig  # noqa: E402


def test_every_item_is_scored_by_some_layer_or_is_clean() -> None:
    units = corpus.must_cover()

    assert len(units) == 38
    for unit in units:
        assert bool(unit.catchers) == (unit.expect == "injection"), unit.item_id


def test_the_data_only_items_are_not_the_classifiers() -> None:
    by_id = {u.item_id: u for u in corpus.must_cover()}

    for item_id in ("export-via-ticket", "write-bulk-drip", "ticket-injection-export"):
        assert by_id[item_id].expect == "injection"  # the overall label is unchanged
        assert by_id[item_id].catchers == ("egress", "canary")
    assert sum("classifier" in u.catchers for u in by_id.values()) == 21


def test_a_missing_or_unknown_catcher_is_refused() -> None:
    item = {"id": "x", "expect": "injection"}

    with pytest.raises(ValueError, match="catchers"):
        corpus._catchers("attacks.toml", item)
    with pytest.raises(ValueError, match="catchers"):
        corpus._catchers("attacks.toml", {**item, "catchers": ["magic"]})
    with pytest.raises(ValueError, match="hostile"):
        corpus._catchers("attacks.toml", {**item, "catchers": []})


# -- the red-team strings: redteam.toml, and the static strings of the attack files --------------


def _config(**changes: Any) -> JudgeConfig:
    return dataclasses.replace(corpus.judge_config(), **changes)


def test_the_red_team_items_are_scored_by_the_same_rule_and_are_not_in_the_first_38() -> None:
    items = corpus.redteam_items()

    assert {i.item_id for i in items} == {
        "obf-zero-width",
        "obf-underscores",
        "obf-zero-width-in-words",
        "obf-long-boundary",
        "lab-notice",
        "lab-summary",
        "lab-record",
    }
    for item in items:
        assert bool(item.catchers) == (item.expect == "injection"), item.item_id
    assert len(corpus.must_cover()) == 38, "the original corpus is unchanged"
    assert {u.source for u in corpus.must_cover()} == {
        "attacks.toml",
        "benign.toml",
        "story_09.toml",
    }


def test_a_long_item_is_recorded_as_the_units_both_rules_cut_it_into() -> None:
    config = corpus.judge_config()
    units = [u for u in corpus.redteam_corpus() if u.item_id == "obf-long-boundary"]
    whole = next(i for i in corpus.redteam_items() if i.item_id == "obf-long-boundary").text

    assert len(whole) > config.max_unit_chars
    assert all(len(u.text) <= config.max_unit_chars for u in units)
    assert len({u.text for u in units}) == len(units)
    legacy = corpus.units_of_text(whole, _config(short_text="legacy", unit_overlap_chars=0))
    current = corpus.units_of_text(whole, config)
    assert {u.text for u in units} == set(legacy) | set(current)
    assert len(set(legacy) | set(current)) > len(legacy), "the overlap makes units v0.1.0 never had"


def test_the_short_obfuscated_items_are_judged_under_the_new_rule_and_not_under_the_old() -> None:
    legacy = _config(short_text="legacy", unit_overlap_chars=0)
    current = corpus.judge_config()
    by_id = {i.item_id: i for i in corpus.redteam_items()}

    for item_id in ("obf-zero-width", "obf-underscores"):
        text = by_id[item_id].text
        assert corpus.units_of_text(text, legacy) == [], item_id
        assert corpus.units_of_text(text, current) == [text], item_id
    spaced = by_id["obf-zero-width-in-words"].text
    assert corpus.units_of_text(spaced, legacy) == [spaced]


def test_the_static_strings_of_the_attack_files_are_the_writes_and_never_the_reads() -> None:
    strings = corpus.attack_strings()
    texts = {u.text for u in strings}

    assert strings, "the attack files were not read"
    ticket = next(i for i in corpus.must_cover() if i.item_id == "ticket-damaged-order")
    surfaces = {u.surface for u in strings if u.text == ticket.text}
    assert surfaces == {"tool_arguments", "tool_result"}, "a written text is judged both ways"
    assert "Supply" not in texts, "the arguments of a read are not judged"
    assert not [t for t in texts if "{" in t], "no template is left unfilled"


def test_everything_to_record_includes_the_red_team_strings() -> None:
    everything = {(u.surface, u.text) for u in corpus.everything()}

    for unit in [*corpus.redteam_corpus(), *corpus.attack_strings()]:
        assert (unit.surface, unit.text) in everything


@pytest.mark.anyio
async def test_the_recorder_reports_each_red_team_item_under_both_rules(
    capsys: pytest.CaptureFixture[str],
) -> None:
    import record_classifier
    from ai_gateway.classifier.judge import Judge
    from ai_gateway.classifier.prompt import prepare  # noqa: F401
    from tests.classifier_helpers import FakeClient

    judge = Judge(
        FakeClient(),
        dataclasses.replace(corpus.judge_config(), max_calls_per_minute_per_client=10_000),
    )

    await record_classifier._report_red_team(judge)

    lines = capsys.readouterr().out.splitlines()
    by_item = {line.split()[0]: line for line in lines[1:]}
    assert set(by_item) == {i.item_id for i in corpus.redteam_items()}
    assert "v0.1.0 rule: not judged" in by_item["obf-zero-width"]
    assert "current rule: 0 of 1 units flagged" in by_item["obf-zero-width"]
    long = by_item["obf-long-boundary"]
    assert "v0.1.0 rule:" in long
    assert "current rule:" in long
