"""The scorecard runner's pure parts: what each column asks of Compose, how attacks are grouped so
that they can run at once without touching each other, what each column predicts, and how a run is
turned into an observation. The parts that need a stack are exercised by `make scorecard`.
Harborline Supply Co. is fictional."""

import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from redteam import scorecard_run as run  # noqa: E402
from redteam.attack_format import load_attack  # noqa: E402
from redteam.execute import Ran  # noqa: E402
from redteam.lab_config import COLUMNS  # noqa: E402
from redteam.oracle import Changes, Landed  # noqa: E402
from redteam.prediction import CONFIG, load_effects  # noqa: E402
from redteam.scripted_client import CallRecord  # noqa: E402
from redteam.stack import redact  # noqa: E402

ATTACKS = {
    p.stem: load_attack(p)
    for p in sorted((ROOT / "scripts" / "redteam" / "attacks").glob("*.toml"))
}
COLS = {c.id: c for c in COLUMNS}
EFFECTS = load_effects(CONFIG / "tool_policies.toml", CONFIG / "lab" / "tool_policies.lab.toml")


def test_a_column_points_the_lab_gateway_at_its_own_files_and_nothing_else() -> None:
    env = run.column_env(COLS["off-egress"])

    assert env["GATEWAY_PIPELINE_FILE_IN_CONTAINER"] == "/app/config/lab/pipelines/off-egress.toml"
    assert env["GATEWAY_ALLOWLIST_FILE_IN_CONTAINER"] == "/app/config/lab/allowlist.lab.toml"
    assert env["GATEWAY_TOOL_PINS_FILE_IN_CONTAINER"] == "/app/config/lab/tool_pins.lab.toml"
    assert (
        env["GATEWAY_APPROVAL_ROLES_FILE_IN_CONTAINER"] == "/app/config/lab/approval_roles.lab.toml"
    )
    assert env["GATEWAY_CLASSIFIER_FILE_IN_CONTAINER"] == "/app/config/classifier.toml"
    assert env["LAB_APPROVER_DECISION"] == "approve"
    assert int(env["GATEWAY_CATALOG_REFRESH_S"]) <= 5


def test_the_before_columns_and_the_denying_column_change_exactly_what_they_say() -> None:
    assert run.column_env(COLS["before-short-text"])["GATEWAY_CLASSIFIER_FILE_IN_CONTAINER"] == (
        "/app/config/lab/classifier.legacy-short-text.toml"
    )
    assert run.column_env(COLS["before-all-flags"])[
        "GATEWAY_CLASSIFIER_FILE_IN_CONTAINER"
    ].endswith("classifier.legacy-both.toml")
    assert run.column_env(COLS["denying-approver"])["LAB_APPROVER_DECISION"] == "reject"
    assert run.column_env(COLS["all-on"])["GATEWAY_PIPELINE_FILE_IN_CONTAINER"].endswith(
        "all-on.toml"
    )


def test_every_column_file_the_runner_names_exists_in_the_repository() -> None:
    for column in COLUMNS:
        for key, value in run.column_env(column).items():
            if key.endswith("_IN_CONTAINER"):
                assert (ROOT / value.removeprefix("/app/")).is_file(), (column.id, value)


def test_attacks_are_grouped_so_that_concurrent_ones_share_no_state() -> None:
    plan = run.plan(ATTACKS.values())

    snapshot = {a.id for a in plan.snapshot}
    assert snapshot == {
        "unauth-status-change",
        "unauth-assign-ticket",
        "unauth-urgent-ticket",
        "out-of-scope-write-comment",
    }, "attacks judged by a diff of the ticketing data run alone, one at a time"
    assert all(a.lab_phase is None for a in plan.product)
    assert not snapshot & {a.id for a in plan.product}
    assert {phase: len(group) for phase, group in plan.lab.items()} == {
        "reviewed": 5,
        "rugpulled": 2,
        "poisoned": 2,
    }
    everything = [*plan.product, *plan.snapshot, *(a for g in plan.lab.values() for a in g)]
    assert sorted(a.id for a in everything) == sorted(ATTACKS), (
        "every attack is in exactly one group"
    )


def test_the_lab_phases_run_in_a_fixed_order_starting_from_reviewed() -> None:
    assert list(run.plan(ATTACKS.values()).lab) == ["reviewed", "rugpulled", "poisoned"]


def test_a_column_predicts_with_the_layers_it_weakens_and_the_approver_it_uses() -> None:
    drip = ATTACKS["exfil-drip-over-window"]
    bulk = ATTACKS["exfil-bulk-write"]

    assert run.predict(COLS["all-on"], bulk, EFFECTS) is False
    assert run.predict(COLS["off-egress"], bulk, EFFECTS) is True
    assert run.predict(COLS["off-canary"], bulk, EFFECTS) is False
    assert run.predict(COLS["all-off"], bulk, EFFECTS) is True
    assert run.predict(COLS["monitor"], bulk, EFFECTS) is True
    assert run.predict(COLS["denying-approver"], drip, EFFECTS) is False, "no write is approved"
    assert run.predict(COLS["all-on"], drip, EFFECTS) is True, "the recorded gap"


def test_the_before_columns_predict_what_all_on_predicts_and_so_show_the_flags_as_differences() -> (
    None
):
    zero_width = ATTACKS["obfuscated-zero-width"]

    for column in (
        "before-short-text",
        "before-overlap",
        "before-validate-results",
        "before-all-flags",
    ):
        assert run.predict(COLS[column], zero_width, EFFECTS) == run.predict(
            COLS["all-on"], zero_width, EFFECTS
        )


def _ran(attack_id: str, outcomes: list[str]) -> Ran:
    attack = ATTACKS[attack_id]
    calls = attack.calls()
    records = [CallRecord(s.id, i, s.tool, o) for (s, i), o in zip(calls, outcomes, strict=False)]
    rows: list[dict[str, Any]] = [
        {
            "tool": r.tool,
            "outcome": "blocked" if r.outcome == "refused" else "forwarded",
            "blocked_by": None,
            "duration_ms": 5.0,
            "verdicts": {},
        }
        for r in records
    ]
    return Ran(attack, records, rows, Landed((), 0, 0), None, False)


def test_an_observation_judges_success_by_the_oracle_never_by_the_gateways_refusal() -> None:
    attack = ATTACKS["obfuscated-zero-width"]
    refused = _ran("obfuscated-zero-width", ["refused"])
    refused.rows[0].update(blocked_by="classifier")
    answered = _ran("obfuscated-zero-width", ["answered"])

    stopped = run.observe(COLS["all-on"], attack, refused, lab_effects=None, effects=EFFECTS)
    leaked = run.observe(COLS["all-on"], attack, answered, lab_effects=None, effects=EFFECTS)

    assert stopped.success is False
    assert stopped.blocked_by == {"classifier": 1}
    assert leaked.success is True, "the read was answered: the client was handed the text"
    assert leaked.predicted is False


def test_would_blocks_and_unclassified_calls_are_counted_from_telemetry() -> None:
    ran = _ran("exfil-bulk-write", ["answered"] * 12 + ["answered"])
    ran.rows[-1]["verdicts"] = {"egress": "would_block", "classifier": "unclassified"}
    ran.rows[3]["verdicts"] = {"classifier": "unclassified"}

    seen = run.observe(
        COLS["monitor"], ATTACKS["exfil-bulk-write"], ran, lab_effects=None, effects=EFFECTS
    )

    assert seen.would_block == {"egress": 1}
    assert seen.unclassified == 2
    assert seen.calls == 13


def test_a_lab_attack_is_judged_by_what_the_lab_upstream_says_it_executed() -> None:
    attack = ATTACKS["smuggle-extra-argument"]
    ran = _ran("smuggle-extra-argument", ["answered"])

    ran_it = run.observe(COLS["off-schema"], attack, ran, lab_effects=1, effects=EFFECTS)
    never = run.observe(COLS["all-on"], attack, ran, lab_effects=0, effects=EFFECTS)

    assert ran_it.success is True
    assert never.success is False
    with pytest.raises(ValueError, match="lab"):
        run.observe(COLS["all-on"], attack, ran, lab_effects=None, effects=EFFECTS)


def test_an_unauthorized_write_is_judged_by_the_snapshot_diff() -> None:
    attack = ATTACKS["unauth-status-change"]
    ran = _ran("unauth-status-change", ["refused"])
    changed = _ran("unauth-status-change", ["answered"])
    changed.changes = Changes(1, 0, 0, 0)
    ran.changes = Changes(0, 0, 0, 0)

    assert (
        run.observe(COLS["all-on"], attack, ran, lab_effects=None, effects=EFFECTS).success is False
    )
    assert (
        run.observe(COLS["off-scope"], attack, changed, lab_effects=None, effects=EFFECTS).success
        is True
    )


def test_an_attack_cut_short_says_so() -> None:
    ran = _ran("exfil-bulk-write", ["answered"] * 3)
    ran.incomplete = True

    seen = run.observe(
        COLS["all-on"], ATTACKS["exfil-bulk-write"], ran, lab_effects=None, effects=EFFECTS
    )

    assert seen.incomplete is True


def test_the_lab_effect_count_for_a_clone_sums_the_landing_tools_it_called() -> None:
    attack = ATTACKS["rug-pull-description"]
    snapshot = {
        "by_client": {
            "harborline-lab-bot--rug-pull-description": {
                "calls": {"fetch_notice": 1, "forward_note": 2, "fetch_report": 5}
            }
        }
    }

    assert run.lab_effects_of(attack, "harborline-lab-bot--rug-pull-description", snapshot) == 3
    assert run.lab_effects_of(attack, "someone-else", snapshot) == 0


def test_a_token_in_the_output_of_a_command_is_never_what_the_runner_prints() -> None:
    assert redact("Bearer aig_abcd1234_" + "x" * 40 + " and more") == "Bearer [token] and more"
    assert redact("plain text") == "plain text"


ALL_THREE = {"LAB_AUTO_APPROVE": "yes", "LAB_MUTABLE_UPSTREAM": "yes", "LAB_FLOOR_OVERRIDE": "yes"}


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"LAB_AUTO_APPROVE": "yes"},
        {"LAB_MUTABLE_UPSTREAM": "yes", "LAB_FLOOR_OVERRIDE": "yes"},
        {"LAB_AUTO_APPROVE": "yes", "LAB_MUTABLE_UPSTREAM": "yes"},
        {**ALL_THREE, "LAB_FLOOR_OVERRIDE": "true"},
        {**ALL_THREE, "LAB_MUTABLE_UPSTREAM": "YES"},
        {**ALL_THREE, "LAB_AUTO_APPROVE": "yes "},
    ],
)
def test_the_runner_refuses_to_start_unless_the_caller_switched_all_three_lab_tools_on(
    env: dict[str, str],
) -> None:
    """The switches are the caller's to turn on, as for every lab script: nothing here sets them."""
    with pytest.raises(SystemExit) as stopped:
        run.require_switches(env)

    for name in ("LAB_AUTO_APPROVE", "LAB_MUTABLE_UPSTREAM", "LAB_FLOOR_OVERRIDE"):
        assert f"{name}=yes" in str(stopped.value)


def test_all_three_switches_exactly_yes_let_the_runner_start() -> None:
    run.require_switches(ALL_THREE)


def test_the_runner_never_sets_any_switch_itself() -> None:
    text = (ROOT / "scripts" / "redteam" / "scorecard_run.py").read_text(encoding="utf-8")
    code = "\n".join(line.split("#", 1)[0] for line in text.splitlines())

    assert '"LAB_AUTO_APPROVE":' not in code
    assert '"LAB_MUTABLE_UPSTREAM":' not in code
    assert '"LAB_FLOOR_OVERRIDE":' not in code


# -- the Docker side: what the runner refuses, and the shape of what it runs ---------------------


def test_the_runner_never_names_the_real_project_and_always_passes_the_project_flag() -> None:
    from redteam.stack import Stack

    for name in ("ai-gateway", "ai-gateway-demo", "ai-gateway-verify"):
        with pytest.raises(ValueError, match="holds data"):
            Stack(ROOT, name, {})
    stack = Stack(ROOT, "ai-gateway-scorecard", {})

    assert stack.command("up", "-d")[:5] == [
        "docker",
        "compose",
        "-p",
        "ai-gateway-scorecard",
        "--profile",
    ]


def test_the_approver_must_say_it_will_do_what_the_column_asks() -> None:
    from redteam.stack import approver_ready_line

    assert (
        approver_ready_line("approve")
        in "READY: lab approver lab-approver will approve every pending write"
    )
    assert (
        approver_ready_line("reject")
        not in "READY: lab approver lab-approver will approve every pending write"
    )
    assert (
        approver_ready_line("reject")
        in "READY: lab approver lab-approver will reject every pending write"
    )


def test_the_owner_url_quotes_a_password_with_awkward_characters() -> None:
    url = run.owner_url(
        {"POSTGRES_USER": "owner", "POSTGRES_PASSWORD": "p@ss/w:rd#1", "POSTGRES_DB": "db"}
    )

    assert url == "postgresql://owner:p%40ss%2Fw%3Ard%231@127.0.0.1:4402/db"


def test_a_check_with_no_committed_scorecard_fails_before_any_stack_is_started(
    tmp_path: Path,
) -> None:
    with pytest.raises(SystemExit) as stopped:
        run.require_committed(tmp_path / "scorecard.json")

    assert "make scorecard" in str(stopped.value)
