"""The traffic simulator's plan: repeatable, and made of calls that mean what they claim."""

import importlib.util
import sys
from collections import Counter
from pathlib import Path

import pytest

from ai_gateway.admin.cli import DEMO_SCOPES_OPS, DEMO_SCOPES_SUPPORT

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "simulate_traffic.py"


@pytest.fixture(scope="module")
def sim():  # type: ignore[no-untyped-def]  # the module is loaded from a script path
    sys.path.insert(0, str(SCRIPT.parent))  # the script imports its neighbour, auto_approver
    spec = importlib.util.spec_from_file_location("simulate_traffic", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


SCOPES = {
    "harborline-support-bot": set(DEMO_SCOPES_SUPPORT),
    "harborline-ops-bot": set(DEMO_SCOPES_OPS),
}
WRITE_TOOLS = {
    "tickets__create_ticket",
    "tickets__add_comment",
    "tickets__change_status",
    "tickets__assign",
}


def test_the_same_seed_gives_the_same_plan_and_another_seed_a_different_one(sim) -> None:  # type: ignore[no-untyped-def]
    assert sim.build_plan(7, 200) == sim.build_plan(7, 200)
    assert sim.build_plan(7, 200) != sim.build_plan(8, 200)


def test_the_mix_is_close_to_the_target_over_many_calls(sim) -> None:  # type: ignore[no-untyped-def]
    kinds = Counter(step.kind for step in sim.build_plan(sim.DEFAULT_SEED, 5000))

    for kind, share in sim.MIX.items():
        assert kinds[kind] / 5000 == pytest.approx(share, abs=0.03), kind


def test_every_normal_call_is_inside_the_scope_of_the_bot_that_sends_it(sim) -> None:  # type: ignore[no-untyped-def]
    plan = sim.build_plan(sim.DEFAULT_SEED, 2000)

    normal = [step for step in plan if step.kind == "normal"]
    assert normal
    assert [s for s in normal if s.tool not in SCOPES[s.client]] == []


def test_every_refused_call_is_outside_the_scope_of_the_bot_that_sends_it(sim) -> None:  # type: ignore[no-untyped-def]
    plan = sim.build_plan(sim.DEFAULT_SEED, 2000)

    refused = [step for step in plan if step.kind == "out_of_scope"]
    assert refused
    assert [s for s in refused if s.tool in SCOPES[s.client]] == []
    assert {s.tool for s in refused} >= {"tickets__change_status", "crm__delete_account"}
    assert all(s.client == "harborline-support-bot" for s in refused if s.tool in sim.OPS_ONLY)


def test_no_writes_means_no_write_tool_is_called(sim) -> None:  # type: ignore[no-untyped-def]
    with_writes = sim.build_plan(sim.DEFAULT_SEED, 1000)
    read_only = sim.build_plan(sim.DEFAULT_SEED, 1000, writes=False)

    assert WRITE_TOOLS & {s.tool for s in with_writes if s.kind == "normal"}
    assert not WRITE_TOOLS & {s.tool for s in read_only if s.kind == "normal"}


def test_no_call_asks_for_a_restricted_handbook_document(sim) -> None:  # type: ignore[no-untyped-def]
    documents = {
        s.arguments["document_id"]
        for s in sim.build_plan(sim.DEFAULT_SEED, 3000)
        if s.tool == "handbook__get_document"
    }

    assert documents
    assert documents.isdisjoint({"DOC-023", "DOC-024", "DOC-030"})


def test_the_expected_counts_add_up_to_the_plan_plus_one_listing_per_bot(sim) -> None:  # type: ignore[no-untyped-def]
    plan = sim.build_plan(sim.DEFAULT_SEED, 300)

    counts = sim.expected_counts(plan)

    assert sum(counts.values()) == 300 + 2
    assert counts[("tools_list", "harborline-ops-bot")] == 1
    assert sum(n for key, n in counts.items() if key[0] == "auth_failure") == sum(
        s.kind == "auth_failure" for s in plan
    )


def test_the_audit_log_is_expected_to_hold_every_call_every_write_and_its_approval(sim) -> None:  # type: ignore[no-untyped-def]
    plan = sim.build_plan(sim.DEFAULT_SEED, 600)

    expected = sim.expected_audit(plan)

    calls = [step for step in plan if step.kind != "auth_failure"]
    forwarded_writes = [
        step for step in plan if step.kind == "normal" and step.tool in sim.WRITE_TOOLS
    ]
    assert expected["gateway.tool_call"] == len(calls)
    assert expected["gateway.call_started"] == len(forwarded_writes) > 0
    for action in ("approval.requested", "approval.resolved", "approval.consumed"):
        assert expected[action] == len(forwarded_writes), "one approval of each kind per write"
    assert set(expected) == {
        "gateway.tool_call",
        "gateway.call_started",
        "approval.requested",
        "approval.resolved",
        "approval.consumed",
    }


def test_a_write_the_scope_layer_refuses_has_no_record_of_an_attempt_to_run(sim) -> None:  # type: ignore[no-untyped-def]
    refused_writes = [
        step
        for step in sim.build_plan(sim.DEFAULT_SEED, 2000)
        if step.kind == "out_of_scope" and step.tool in sim.WRITE_TOOLS
    ]

    assert refused_writes
    assert sim.expected_audit(refused_writes) == {"gateway.tool_call": len(refused_writes)}


def test_each_kind_of_failed_authentication_is_expected_under_its_own_reason(sim) -> None:  # type: ignore[no-untyped-def]
    plan = sim.build_plan(sim.DEFAULT_SEED, 2000)

    reasons = {key[1] for key in sim.expected_counts(plan) if key[0] == "auth_failure"}

    assert reasons == {"missing", "malformed", "unknown_token", "wrong_secret"}
