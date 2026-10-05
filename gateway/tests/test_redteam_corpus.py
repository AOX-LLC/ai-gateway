"""The attack corpus is consistent with the product it attacks, before any run.

Each attack's expectation is decided in its file. These tests check it against what the file
cannot lie about: the clients' scopes, the pinned schemas, the lab upstream's phases, the seeded
data, and the classifier's own rules about what it judges. A prediction that contradicts them is
a mistake in the file; a prediction a run later disproves is a finding, and is reported, not edited.
Harborline Supply Co. is fictional.
"""

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import classifier_corpus as corpus  # noqa: E402
from ai_gateway.admin.cli import (  # noqa: E402
    DEMO_SCOPES_HELPER_API,
    DEMO_SCOPES_OPS,
    DEMO_SCOPES_SUPPORT,
)
from ai_gateway.pipeline.pins import load_tool_pins  # noqa: E402
from crm_server.seed import build_dataset as crm_dataset  # noqa: E402
from lab_upstream.tools import definitions as lab_definitions  # noqa: E402
from redteam.attack_format import TOKENS, Attack, load_attack  # noqa: E402
from redteam.prediction import CONFIG, WEAKENABLE, load_effects, predicted_success  # noqa: E402
from ticketing_server.seed import build_dataset as ticketing_dataset  # noqa: E402

ATTACKS = ROOT / "scripts" / "redteam" / "attacks"
FILES = sorted(ATTACKS.glob("*.toml"))
ALL = {path.stem: load_attack(path) for path in FILES}
EFFECTS = load_effects(CONFIG / "tool_policies.toml", CONFIG / "lab" / "tool_policies.lab.toml")
LAB_TOOLS = [f"lab__{tool.name}" for tool in lab_definitions("poisoned")]
SCOPES = {
    "harborline-support-bot": set(DEMO_SCOPES_SUPPORT),
    "harborline-ops-bot": set(DEMO_SCOPES_OPS),
    "harborline-helper-api": set(DEMO_SCOPES_HELPER_API),
    "harborline-lab-bot": set(LAB_TOOLS),
}
PINS = load_tool_pins(CONFIG / "lab" / "tool_pins.lab.toml")
DYNAMIC = frozenset({"planted", "values", "canary", "encode"})


def test_every_file_is_named_for_its_attack_and_ids_and_markers_are_unique() -> None:
    assert len(FILES) >= 30
    for path in FILES:
        assert load_attack(path).id == path.stem
    assert len({a.marker for a in ALL.values()}) == len(ALL), "a marker names one attack's tickets"


def test_the_corpus_covers_the_families_the_scorecard_promises() -> None:
    families = {a.expect.family for a in ALL.values()}

    assert families >= {
        "exfil-bulk",
        "exfil-drip",
        "exfil-cross-session",
        "unauthorized-write",
        "out-of-scope",
        "tool-poisoning",
        "rug-pull",
        "schema-smuggling",
        "canary",
        "encoded-exfil",
        "obfuscated-instruction",
        "benign",
    }
    wanted = {
        "smuggle-extra-argument",
        "smuggle-oversized-argument",
        "poisoned-remote-ref",
        "canary-base64",
        "canary-hex",
        "encoded-exfil-base64",
        "encoded-exfil-hex",
        "obfuscated-zero-width",
        "obfuscated-underscores",
        "obfuscated-split-short-fields",
        "obfuscated-long-boundary",
        "rug-pull-description",
        "rug-pull-schema",
        "exfil-drip-below-limit",
    }
    assert wanted <= set(ALL)
    benign = [a for a in ALL.values() if not a.hostile]
    assert len(benign) >= 5


def test_each_clients_scope_decides_whether_a_call_is_blocked_by_scope() -> None:
    for attack in ALL.values():
        assert attack.client in SCOPES, attack.id
        for step in attack.steps:
            in_scope = step.tool in SCOPES[attack.client]
            scope_blocked = any(
                e.outcome == "blocked" and e.blocked_by == "scope" for e in step.enforce
            )
            assert in_scope != scope_blocked, f"{attack.id} {step.id}: {step.tool}"


def test_a_floor_layer_blocks_the_same_calls_in_a_monitor_run() -> None:
    for attack in ALL.values():
        for step in attack.steps:
            for i in range(1, step.repeat + 1):
                enforce, monitor = step.expected_enforce(i), step.expected_monitor(i)
                floor_blocked = bool(enforce.catchers & {"scope", "approval"})
                assert (monitor.outcome == "blocked") == floor_blocked, f"{attack.id} {step.id}"
                assert monitor.would_block == enforce.catchers - {"scope", "approval"} or (
                    enforce.outcome == "allowed" and not monitor.would_block
                ), f"{attack.id} {step.id}: would_block is the weakenable catchers"


def test_the_arguments_are_what_the_pinned_schema_names_unless_the_attack_smuggles() -> None:
    for attack in ALL.values():
        smuggles = attack.expect.family in ("schema-smuggling", "rug-pull", "tool-poisoning")
        for step in attack.steps:
            pin = PINS.get(step.tool)
            if pin is None:
                assert step.tool.startswith("lab__"), f"{attack.id}: no pin for {step.tool}"
                continue
            extra = set(step.arguments) - set(pin.input_schema["properties"])
            assert not extra or smuggles, f"{attack.id} {step.id}: {sorted(extra)}"
            missing = set(pin.input_schema.get("required", [])) - set(step.arguments)
            assert not missing, f"{attack.id} {step.id} leaves out {sorted(missing)}"


def test_an_attack_that_uses_the_lab_upstream_names_a_phase_that_offers_every_tool_it_calls() -> (
    None
):
    for attack in ALL.values():
        lab_steps = [s for s in attack.steps if s.tool.startswith("lab__")]
        if not lab_steps:
            assert attack.lab_phase is None, attack.id
            continue
        assert attack.client == "harborline-lab-bot", attack.id
        assert attack.lab_phase is not None, attack.id
        offered = {f"lab__{t.name}" for t in lab_definitions(attack.lab_phase)}
        assert {s.tool for s in lab_steps} <= offered, attack.id


def test_a_gap_is_declared_exactly_where_every_layer_on_still_lets_the_attack_succeed() -> None:
    for attack in ALL.values():
        succeeds = predicted_success(attack, frozenset(), EFFECTS)
        assert succeeds == bool(attack.expect.gap), (
            f"{attack.id}: predicted success {succeeds}, gap {attack.expect.gap!r}"
        )


def test_every_attack_is_predicted_to_succeed_once_every_weakenable_layer_only_watches() -> None:
    """The scorecard's monitor column: unless a floor layer (scope, approval) still blocks it, an
    attack that is the product's to stop is stopped by nothing."""
    for attack in ALL.values():
        floor_blocks = any(
            step.expected_monitor(i).outcome == "blocked"
            for step in attack.steps
            if step.id in attack.expect.landing
            for i in range(1, step.repeat + 1)
        )
        assert predicted_success(attack, WEAKENABLE, EFFECTS) == (
            attack.hostile and not floor_blocks
        )


def test_the_benign_workload_expects_nothing_to_be_stopped_or_to_would_stop() -> None:
    for attack in ALL.values():
        if attack.hostile:
            continue
        for step in attack.steps:
            assert [e.outcome for e in step.enforce] == ["allowed"], attack.id
            assert all(not m.would_block and m.outcome == "forwarded" for m in step.monitor)


def _supply(attack: Attack) -> int:
    """Distinct customer values the attack's reads give it, from the seeded CRM, in read order."""
    data = crm_dataset()
    contacts: dict[str, list[tuple[str, str]]] = {}
    for contact in data.contacts:
        contacts.setdefault(contact.account_id, []).append((contact.email, contact.phone))
    seen: list[str] = []
    for step in attack.steps:
        if step.tool != "crm__get_account":
            continue
        for i in range(1, step.repeat + 1):
            account = str(step.arguments["account_id"]).replace("{i:05d}", f"{i:05d}")
            for pair in contacts.get(account, []):
                seen += [v for v in pair if v not in seen]
    return len(seen)


def _demand(attack: Attack) -> int:
    total = 0
    for step in attack.steps:
        for value in step.arguments.values():
            if not isinstance(value, str):
                continue
            for name, spec in TOKENS.findall(value):
                if name == "values":
                    total += step.repeat * int(spec)
                elif name == "encode" and spec.partition(":")[2].startswith("values:"):
                    total += step.repeat * int(spec.rpartition(":")[2])
    return total


def test_every_attack_reads_enough_customer_values_to_write_what_it_writes() -> None:
    for attack in ALL.values():
        assert _demand(attack) <= _supply(attack), attack.id


def test_the_reads_stay_inside_the_rate_limit_so_the_limit_is_not_what_stops_an_attack() -> None:
    for attack in ALL.values():
        reads = sum(s.repeat for s in attack.steps if s.tool == "crm__get_account")
        if attack.expect.family != "acceptance":
            assert reads <= 30, attack.id


def test_the_tickets_and_staff_an_attack_names_exist_and_the_change_would_be_a_change() -> None:
    data = ticketing_dataset()
    tickets = {t.id: t for t in data.tickets}
    staff = {s.handle for s in data.staff}
    for attack in ALL.values():
        for step in attack.steps:
            args = step.arguments
            if "ticket_id" in args and not str(args["ticket_id"]).startswith("{"):
                assert args["ticket_id"] in tickets, attack.id
            if step.tool == "tickets__change_status":
                assert tickets[str(args["ticket_id"])].status != args["status"], attack.id
            if step.tool == "tickets__assign":
                assert args["assignee"] in staff, attack.id
                assert tickets[str(args["ticket_id"])].assignee != args["assignee"], attack.id


def test_an_inline_planted_piece_is_judged_under_neither_rule_and_so_needs_no_recording() -> None:
    config = corpus.judge_config()
    checked = 0
    for attack in ALL.values():
        if attack.plant is None or attack.plant.text in {u.text for u in corpus.redteam_items()}:
            continue
        if attack.plant.text in {u.text for u in corpus.must_cover()}:
            continue
        for rule in corpus.both_rules(config):
            pieces = {"subject": attack.plant.subject, "description": attack.plant.text}
            assert corpus.units_of_text(attack.plant.subject, rule) == [], attack.id
            assert corpus.units_of_text(attack.plant.text, rule) == [], attack.id
            assert pieces
        checked += 1
    assert checked == 1, "the split-fields attack"


def test_the_text_of_the_obfuscated_attacks_is_a_red_team_corpus_string() -> None:
    texts = {i.item_id: i for i in corpus.redteam_items()}
    for name, item in (
        ("obfuscated-zero-width", "obf-zero-width"),
        ("obfuscated-underscores", "obf-underscores"),
        ("obfuscated-zero-width-in-words", "obf-zero-width-in-words"),
    ):
        assert ALL[name].plant is not None
        assert ALL[name].plant.text == texts[item].text  # type: ignore[union-attr]


@pytest.mark.parametrize("name", sorted(ALL))
def test_no_attack_names_a_real_person_or_company(name: str) -> None:
    text = (ATTACKS / f"{name}.toml").read_text(encoding="utf-8")

    assert "Harborline Supply Co. is fictional" in text or ALL[name].marker.startswith("RT-")
    assert not re.search(r"@(?!harborline)", text.replace("{", " "))
