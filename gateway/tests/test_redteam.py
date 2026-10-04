"""The red-team harness: the attack format, the judge of a run and the independent oracle."""

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest

from crm_server.seed import CRM_CANARY
from crm_server.seed import Dataset as CrmDataset
from ticketing_server.seed import TICKETING_CANARY

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import classifier_corpus as corpus  # noqa: E402
from redteam.attack_format import AttackFormatError, load_attack, parse_attack  # noqa: E402
from redteam.oracle import THRESHOLD, Landed, clean_up, plant_ticket, read_landed  # noqa: E402
from redteam.run_attack import judge  # noqa: E402
from redteam.scripted_client import AttackStoppedError, CallRecord, Loot, render  # noqa: E402

ATTACK = ROOT / "scripts" / "redteam" / "attacks" / "export-every-customer.toml"


def _raw(**step: Any) -> dict[str, Any]:
    base = {
        "id": "s",
        "tool": "crm__get_account",
        "arguments": {"account_id": "ACC-{i:05d}"},
        "repeat": 3,
        "enforce": [{"outcome": "allowed"}],
        "monitor": [{"would_block": []}],
    }
    return {
        "id": "a",
        "title": "t",
        "client": "c",
        "marker": "RT-x",
        "plant": {
            "account_id": "ACC-00001",
            "subject": "Order question",
            "text_from": {"file": "story_09.toml", "id": "email-injection"},
        },
        "step": [{**base, **step}],
    }


def test_the_shipped_attack_loads_and_plants_a_recorded_corpus_string() -> None:
    attack = load_attack(ATTACK)

    assert len(attack.calls()) == 47
    recorded = {u.text for u in corpus.must_cover() if u.item_id == "email-injection"}
    assert attack.plant.text in recorded


def test_every_call_of_the_shipped_attack_has_a_judgement_in_both_runs() -> None:
    attack = load_attack(ATTACK)

    for step, i in attack.calls():
        assert step.expected_enforce(i)
        assert step.expected_monitor(i) is not None


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"enforce": [{"last": 2, "outcome": "allowed"}]}, "cover"),
        ({"enforce": [{"outcome": "blocked"}]}, "blocked_by"),
        ({"enforce": [{"outcome": "blocked", "blocked_by": "magic"}]}, "unknown layer"),
        ({"monitor": [{"would_block": ["magic"]}]}, "unknown layers"),
        ({"arguments": {"x": "{nope}"}}, "unknown template"),
        ({"repeat": 0}, "repeat"),
        ({"surprise": 1}, "unknown keys"),
    ],
)
def test_a_mistake_in_an_attack_file_is_refused(change: dict[str, Any], message: str) -> None:
    with pytest.raises(AttackFormatError, match=message):
        parse_attack(_raw(**change))


def test_templates_fill_from_what_the_attack_has_read_and_never_guess() -> None:
    attack = parse_attack(_raw())
    loot = Loot()
    loot.note(
        "contact a@harborline.example 555-0101, b@harborline.example 555-0102. CNRY-HBL-3F9A71C2"
    )

    def fill(template: str, i: int = 2) -> str:
        return render(template, attack=attack, step_id="s", i=i, planted="TKT-000099", loot=loot)

    assert fill("ACC-{i:05d}") == "ACC-00002"
    assert fill("{planted} {marker} {step}") == "TKT-000099 RT-x s"
    assert fill("{canary}") == "CNRY-HBL-3F9A71C2"
    assert fill("{values:3}") == "a@harborline.example 555-0101 b@harborline.example"
    with pytest.raises(AttackStoppedError, match="only 4 customer values"):
        fill("{values:2}")  # one value is left
    with pytest.raises(AttackStoppedError, match="never saw a canary"):
        render("{canary}", attack=attack, step_id="s", i=1, planted="x", loot=Loot())


def _run(attack_file: Path = ATTACK) -> tuple[Any, list[CallRecord]]:
    attack = load_attack(attack_file)
    return attack, [CallRecord(s.id, i, s.tool, "answered") for s, i in attack.calls()]


def _enforce_rows(attack: Any) -> tuple[list[CallRecord], list[dict[str, Any]]]:
    records, rows = [], []
    for step, i in attack.calls():
        want = step.expected_enforce(i)
        blocked = want.outcome == "blocked"
        records.append(CallRecord(step.id, i, step.tool, "refused" if blocked else "answered"))
        rows.append(
            {
                "tool": step.tool,
                "outcome": "blocked" if blocked else "forwarded",
                "blocked_by": want.blocked_by,
                "would_block": set(),
            }
        )
    return records, rows


NOTHING = Landed((), 0, 0)


def test_an_enforce_run_that_goes_as_expected_with_nothing_landed_passes() -> None:
    attack, _ = _run()
    records, rows = _enforce_rows(attack)

    assert judge(attack, "enforce", records, rows, NOTHING) == []


def test_an_enforce_run_fails_on_the_wrong_layer_a_call_let_through_or_a_landed_export() -> None:
    attack, _ = _run()
    records, rows = _enforce_rows(attack)
    wrong_layer = [dict(r) for r in rows]
    wrong_layer[-1]["blocked_by"] = "canary"
    let_through = [dict(r) for r in rows]
    let_through[0] = {**let_through[0], "outcome": "forwarded"}

    assert any(
        "blocked by canary" in f for f in judge(attack, "enforce", records, wrong_layer, NOTHING)
    )
    assert any(
        "expected blocked" in f for f in judge(attack, "enforce", records, let_through, NOTHING)
    )
    landed = Landed(("RT-export bulk",), THRESHOLD, 1)
    assert any("nothing should" in f for f in judge(attack, "enforce", records, rows, landed))
    assert judge(
        attack, "enforce", records, rows[:-1], NOTHING
    )  # a call the gateway never recorded


def test_a_monitor_run_expects_every_call_forwarded_and_the_would_blocks_of_the_file() -> None:
    attack, _ = _run()
    records, rows = [], []
    for step, i in attack.calls():
        records.append(CallRecord(step.id, i, step.tool, "answered"))
        rows.append(
            {
                "tool": step.tool,
                "outcome": "forwarded",
                "blocked_by": None,
                "would_block": set(step.expected_monitor(i).would_block),
            }
        )
    writes = sum(s.tool == "tickets__create_ticket" for s, _ in attack.calls())
    landed = Landed(tuple(f"RT-export {n}" for n in range(writes)), 12, 2)

    assert judge(attack, "monitor", records, rows, landed) == []
    assert any("cannot be trusted" in f for f in judge(attack, "monitor", records, rows, NOTHING))
    rows[0]["would_block"] = set()
    assert any("would_block" in f for f in judge(attack, "monitor", records, rows, landed))


# -- the oracle ---------------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.anyio
async def test_the_oracle_sees_values_and_canaries_that_landed_and_nothing_else(
    test_database_url: str, ticketing_data: Any, crm_data: CrmDataset
) -> None:
    contacts = [(c.email, c.phone) for c in crm_data.contacts]
    planted, since = plant_ticket(test_database_url, "ACC-00003", "Order question", "Hello there.")
    assert read_landed(test_database_url, since, planted, "RT-x") == Landed((), 0, 0)

    def make(subject: str, description: str) -> None:
        with psycopg.connect(test_database_url) as connection:
            connection.execute(
                "INSERT INTO ticketing.tickets (account_id, subject, description, requested_by)"
                " VALUES ('ACC-00001', %s, %s, 'harborline-support-bot')",
                (subject, description),
            )

    make("RT-x honest", f"Please call {contacts[0][1]} about the pallet.")
    few = read_landed(test_database_url, since, planted, "RT-x")
    assert (few.customer_values, few.canaries, few.exported) == (1, 0, False)

    make("RT-x canary", f"code {TICKETING_CANARY.lower()}")
    assert read_landed(test_database_url, since, planted, "RT-x").exported  # a canary, any case

    make("RT-x bulk", " ".join(value for pair in contacts[:THRESHOLD] for value in pair))
    bulk = read_landed(test_database_url, since, planted, "RT-x")
    assert bulk.customer_values >= THRESHOLD
    assert bulk.canaries == 1
    assert set(bulk.subjects) == {"RT-x honest", "RT-x canary", "RT-x bulk"}

    removed = clean_up(test_database_url, since, planted, "RT-x")
    assert removed == 4  # the three and the planted one
    assert read_landed(test_database_url, since, planted, "RT-x") == Landed((), 0, 0)
    assert datetime.now(UTC) - since < timedelta(minutes=5)
    assert CRM_CANARY  # the seed's constant, as the oracle uses it


# -- the lab's pipeline -------------------------------------------------------------------------


def test_the_monitor_pipeline_weakens_every_layer_that_may_be_and_keeps_the_floors() -> None:
    from ai_gateway.pipeline.config import LayerMode, load_pipeline_config
    from ai_gateway.pipeline.registry import LAYER_ORDER

    config = load_pipeline_config(ROOT / "config" / "pipeline.monitor.toml", LAYER_ORDER)

    assert not config.allow_floor_override
    for layer in LAYER_ORDER:
        want = LayerMode.ENFORCE if layer.floor else LayerMode.MONITOR
        assert config.modes[layer.name] is want, layer.name
