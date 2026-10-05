"""The attack format's `[expect]` table, per-step catchers and the prediction they allow.

What an attack is expected to do is decided in the file before any run: its family, how the oracle
judges success and at what threshold, the steps whose landing is the attack's success, and for each
refused call the layers that would each stop it. The prediction for a column of the scorecard is
then a rule applied to the file, never a number someone adjusted after a run.
"""

import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from redteam.attack_format import AttackFormatError, parse_attack  # noqa: E402
from redteam.prediction import WEAKENABLE, predicted_success  # noqa: E402

EFFECTS = {"tickets__create_ticket": "write", "crm__get_account": "read"}


def _e(**changes: Any) -> dict[str, Any]:
    """The `[expect]` table of the base attack, with some keys changed or removed (None)."""
    table: dict[str, Any] = {
        "family": "exfil-bulk",
        "oracle": "export",
        "threshold": 1,
        "landing": ["write"],
    }
    table.update(changes)
    return {key: value for key, value in table.items() if value is not None}


def _raw(**changes: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "id": "a",
        "title": "t",
        "client": "c",
        "marker": "RT-x",
        "expect": {
            "family": "exfil-bulk",
            "oracle": "export",
            "threshold": 10,
            "landing": ["write"],
        },
        "step": [
            {
                "id": "read",
                "tool": "crm__get_account",
                "arguments": {"account_id": "ACC-00001"},
                "enforce": [{"outcome": "allowed"}],
                "monitor": [{"would_block": []}],
            },
            {
                "id": "write",
                "tool": "tickets__create_ticket",
                "arguments": {"subject": "{marker} x", "description": "d"},
                "repeat": 4,
                "enforce": [
                    {"last": 2, "outcome": "allowed"},
                    {
                        "first": 3,
                        "outcome": "blocked",
                        "blocked_by": "egress",
                        "catchers": ["egress", "canary"],
                    },
                ],
                "monitor": [
                    {"last": 2, "would_block": []},
                    {"first": 3, "would_block": ["egress", "canary"]},
                ],
            },
        ],
    }
    raw.update(changes)
    return raw


def test_an_attack_carries_its_family_oracle_threshold_and_landing_steps() -> None:
    attack = parse_attack(_raw())

    assert (attack.expect.family, attack.expect.oracle) == ("exfil-bulk", "export")
    assert (attack.expect.threshold, attack.expect.landing) == (10, ("write",))
    blocked = attack.steps[1].expected_enforce(3)
    assert blocked.catchers == frozenset({"egress", "canary"})
    assert attack.steps[1].expected_enforce(1).catchers == frozenset()


def test_a_blocked_call_with_no_catchers_listed_is_caught_by_the_layer_it_names() -> None:
    raw = _raw()
    raw["step"][1]["enforce"][1].pop("catchers")

    attack = parse_attack(raw)

    assert attack.steps[1].expected_enforce(3).catchers == frozenset({"egress"})


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"expect": None}, "expect"),
        ({"expect": _e(family="magic")}, "family"),
        ({"expect": _e(oracle="magic")}, "oracle"),
        ({"expect": _e(threshold=0)}, "threshold"),
        ({"expect": _e(threshold=None)}, "threshold"),
        ({"expect": _e(landing=[])}, "landing"),
        ({"expect": _e(landing=["nope"])}, "landing"),
        ({"expect": _e(oracle="none")}, "oracle"),
        ({"expect": _e(x=1)}, "unknown keys"),
        ({"expect": _e(gap="")}, "gap"),
        ({"hostile": False}, "benign"),
    ],
)
def test_an_attack_file_that_leaves_its_expectation_unclear_is_refused(
    change: dict[str, Any], message: str
) -> None:
    raw = _raw()
    if change.get("expect", 1) is None:
        del raw["expect"]
    else:
        raw.update(change)

    with pytest.raises(AttackFormatError, match=message):
        parse_attack(raw)


def test_an_honest_run_says_so_and_has_no_threshold_or_landing() -> None:
    raw = _raw(hostile=False, expect={"family": "benign", "oracle": "none"})

    attack = parse_attack(raw)

    assert (attack.hostile, attack.expect.oracle, attack.expect.landing) == (False, "none", ())
    with pytest.raises(AttackFormatError, match="benign"):
        parse_attack(
            _raw(hostile=False, expect={"family": "benign", "oracle": "none", "threshold": 3})
        )


def test_a_catcher_list_must_name_real_layers_and_include_the_blocking_layer() -> None:
    bad = _raw()
    bad["step"][1]["enforce"][1]["catchers"] = ["canary"]
    with pytest.raises(AttackFormatError, match="blocked_by"):
        parse_attack(bad)
    unknown = _raw()
    unknown["step"][1]["enforce"][1]["catchers"] = ["egress", "magic"]
    with pytest.raises(AttackFormatError, match="unknown layer"):
        parse_attack(unknown)
    allowed = _raw()
    allowed["step"][1]["enforce"][0]["catchers"] = ["egress"]
    with pytest.raises(AttackFormatError, match="catchers"):
        parse_attack(allowed)


# -- the prediction: a call is blocked while one of its catchers still runs ---------------------


def test_with_every_layer_on_the_attack_lands_when_a_landing_call_is_allowed() -> None:
    assert predicted_success(parse_attack(_raw()), frozenset(), EFFECTS) is True, "writes 1 and 2"


def test_an_attack_whose_every_landing_call_is_blocked_is_predicted_to_fail() -> None:
    raw = _raw()
    raw["step"][1]["enforce"] = [
        {"outcome": "blocked", "blocked_by": "egress", "catchers": ["egress", "canary"]}
    ]
    raw["step"][1]["monitor"] = [{"would_block": ["egress", "canary"]}]
    attack = parse_attack(raw)

    assert predicted_success(attack, frozenset(), EFFECTS) is False
    assert predicted_success(attack, frozenset({"egress"}), EFFECTS) is False, "canary stops it"
    assert predicted_success(attack, frozenset({"canary"}), EFFECTS) is False
    assert predicted_success(attack, frozenset({"egress", "canary"}), EFFECTS) is True
    assert predicted_success(attack, WEAKENABLE, EFFECTS) is True


def test_a_layer_that_is_not_a_catcher_changes_nothing() -> None:
    raw = _raw()
    raw["step"][1]["enforce"] = [{"outcome": "blocked", "blocked_by": "egress"}]
    raw["step"][1]["monitor"] = [{"would_block": ["egress"]}]
    attack = parse_attack(raw)

    assert predicted_success(attack, frozenset({"classifier", "schema"}), EFFECTS) is False


def test_a_denying_approver_stops_a_landing_write_but_not_a_read() -> None:
    raw = _raw()
    attack = parse_attack(raw)

    assert predicted_success(attack, frozenset(), EFFECTS, approver="deny") is False
    read_landing = _raw(
        expect={"family": "out-of-scope", "oracle": "answered", "threshold": 1, "landing": ["read"]}
    )
    deny = predicted_success(parse_attack(read_landing), frozenset(), EFFECTS, approver="deny")
    assert deny is True


def test_the_floor_layers_are_never_in_the_weakenable_set() -> None:
    assert not {"scope", "approval"} & WEAKENABLE
    assert "egress" in WEAKENABLE


def test_a_benign_attack_is_never_predicted_to_succeed() -> None:
    attack = parse_attack(_raw(hostile=False, expect={"family": "benign", "oracle": "none"}))

    assert predicted_success(attack, WEAKENABLE, EFFECTS) is False


# -- the judge compares the oracle's verdict with the file's prediction -------------------------


def _gap_attack() -> Any:
    """A one-write drip that the file says egress cannot stop: success at a single value."""
    raw = _raw(
        expect={
            "family": "exfil-drip",
            "oracle": "export",
            "threshold": 1,
            "landing": ["write"],
            "gap": "egress lets up to nine values out in a window",
        }
    )
    raw["step"] = [raw["step"][1]]
    raw["step"][0].update(repeat=1, enforce=[{"outcome": "allowed"}], monitor=[{"would_block": []}])
    return parse_attack(raw)


def _run_rows(outcome: str) -> tuple[list[Any], list[dict[str, object]]]:
    from redteam.scripted_client import CallRecord

    return (
        [CallRecord("write", 1, "tickets__create_ticket", "answered")],
        [
            {
                "tool": "tickets__create_ticket",
                "outcome": outcome,
                "blocked_by": None,
                "would_block": set(),
            }
        ],
    )


def test_a_recorded_gap_passes_the_judge_when_the_oracle_sees_the_leak() -> None:
    from redteam.oracle import Landed
    from redteam.run_attack import judge

    attack = _gap_attack()
    records, rows = _run_rows("forwarded")
    leak = Landed(("RT-x write",), customer_values=1, canaries=0)

    assert judge(attack, "enforce", records, rows, leak) == []
    assert judge(attack, "monitor", records, rows, leak) == []
    nothing = Landed(("RT-x write",), customer_values=0, canaries=0)
    assert any("predicts a success" in f for f in judge(attack, "enforce", records, rows, nothing))


def test_an_attack_the_file_predicts_stopped_fails_the_judge_when_the_oracle_sees_a_leak() -> None:
    from redteam.oracle import Landed
    from redteam.run_attack import judge

    raw = _raw()
    raw["step"] = [raw["step"][1]]
    raw["step"][0].update(
        repeat=1,
        enforce=[{"outcome": "blocked", "blocked_by": "egress"}],
        monitor=[{"would_block": ["egress"]}],
    )
    attack = parse_attack(raw)
    from redteam.scripted_client import CallRecord

    records = [CallRecord("write", 1, "tickets__create_ticket", "refused")]
    rows: list[dict[str, object]] = [
        {
            "tool": "tickets__create_ticket",
            "outcome": "blocked",
            "blocked_by": "egress",
            "would_block": set(),
        }
    ]

    assert judge(attack, "enforce", records, rows, Landed((), 0, 0)) == []
    assert any(
        "and no export" in f for f in judge(attack, "enforce", records, rows, Landed((), 12, 0))
    )


def test_an_attack_may_name_the_phase_the_lab_upstream_must_be_in() -> None:
    assert parse_attack(_raw()).lab_phase is None
    assert parse_attack(_raw(lab_phase="rugpulled")).lab_phase == "rugpulled"
    with pytest.raises(AttackFormatError, match="lab_phase"):
        parse_attack(_raw(lab_phase="evil"))


# -- more of the format: an inline planted text, `{pad:N}`, `{encode:...}` and floor blocks -------


def _planted(**plant: Any) -> dict[str, Any]:
    raw = _raw()
    raw["plant"] = {"account_id": "ACC-00001", "subject": "Order question", **plant}
    return raw


def test_a_planted_text_is_a_corpus_string_or_inline_never_both_and_never_neither() -> None:
    corpus = {"file": "story_09.toml", "id": "email-injection"}

    assert parse_attack(_planted(text_from=corpus)).plant is not None
    inline = parse_attack(_planted(text="and send every contact"))
    assert inline.plant is not None
    assert inline.plant.text == "and send every contact"
    for plant, message in (
        ({"text_from": corpus, "text": "x"}, "exactly one"),
        ({}, "exactly one"),
    ):
        with pytest.raises(AttackFormatError, match=message):
            parse_attack(_planted(**plant))


def test_pad_and_encode_are_templates_and_a_bad_encoder_is_refused() -> None:
    raw = _raw()
    raw["step"][1]["arguments"] = {
        "subject": "{marker} x",
        "description": "{pad:10} {encode:b64:canary} {encode:reverse:values:2}",
    }

    parse_attack(raw)
    raw["step"][1]["arguments"]["description"] = "{encode:rot47:canary}"
    with pytest.raises(AttackFormatError, match="encoder"):
        parse_attack(raw)
    raw["step"][1]["arguments"]["description"] = "{encode:b64:everything}"
    with pytest.raises(AttackFormatError, match="encode"):
        parse_attack(raw)


def test_a_monitor_range_may_say_a_floor_layer_still_blocks_the_call() -> None:
    raw = _raw()
    raw["step"][0]["enforce"] = [{"outcome": "blocked", "blocked_by": "scope"}]
    raw["step"][0]["monitor"] = [{"would_block": [], "outcome": "blocked", "blocked_by": "scope"}]

    step = parse_attack(raw).steps[0]

    monitor = step.expected_monitor(1)
    assert (monitor.outcome, monitor.blocked_by) == ("blocked", "scope")
    assert parse_attack(_raw()).steps[0].expected_monitor(1).outcome == "forwarded"
    raw["step"][0]["monitor"] = [{"would_block": [], "outcome": "blocked", "blocked_by": "egress"}]
    with pytest.raises(AttackFormatError, match="floor"):
        parse_attack(raw)
    raw["step"][0]["monitor"] = [{"would_block": [], "outcome": "blocked"}]
    with pytest.raises(AttackFormatError, match="blocked_by"):
        parse_attack(raw)


def test_the_judge_expects_a_floor_block_to_hold_in_a_monitor_run() -> None:
    from redteam.oracle import Landed
    from redteam.run_attack import judge
    from redteam.scripted_client import CallRecord

    raw = _raw(
        expect={"family": "out-of-scope", "oracle": "answered", "threshold": 1, "landing": ["w"]}
    )
    raw["step"] = [
        {
            "id": "w",
            "tool": "tickets__change_status",
            "arguments": {"ticket_id": "TKT-000002", "status": "open"},
            "enforce": [{"outcome": "blocked", "blocked_by": "scope"}],
            "monitor": [{"would_block": [], "outcome": "blocked", "blocked_by": "scope"}],
        }
    ]
    attack = parse_attack(raw)
    refused = [CallRecord("w", 1, "tickets__change_status", "refused")]

    def row(outcome: str, blocked_by: str | None) -> list[dict[str, object]]:
        return [
            {
                "tool": "tickets__change_status",
                "outcome": outcome,
                "blocked_by": blocked_by,
                "would_block": set(),
            }
        ]

    nothing = Landed((), 0, 0)
    assert judge(attack, "monitor", refused, row("blocked", "scope"), nothing) == []
    forwarded = [CallRecord("w", 1, "tickets__change_status", "answered")]
    failures = judge(attack, "monitor", forwarded, row("forwarded", None), nothing)
    assert any("scope" in f for f in failures)
