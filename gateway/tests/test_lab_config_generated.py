"""The lab-only configuration the scorecard runs against: a client for each attack, a pipeline for
each column, the classifier and result-validation settings of the before columns. All of it is
generated from the attack files and committed, and these tests keep the two the same. Nothing here
changes production code: the allowlist, rate limits and approval roles are read by the same loaders,
and no pattern matching of client names exists anywhere.
Harborline Supply Co. is fictional."""

import dataclasses
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from ai_gateway.pipeline.config import parse_pipeline_config  # noqa: E402
from ai_gateway.pipeline.layers.allowlist import load_allowlist  # noqa: E402
from ai_gateway.pipeline.layers.classifier import load_judge_config  # noqa: E402
from ai_gateway.pipeline.layers.rate_limit import load_rate_limits  # noqa: E402
from ai_gateway.pipeline.registry import LAYER_ORDER  # noqa: E402
from ai_gateway.pipeline.types import LayerMode  # noqa: E402
from redteam import lab_config  # noqa: E402
from redteam.attack_format import LAYERS, load_attack  # noqa: E402

LAB = ROOT / "config" / "lab"
ATTACKS = {
    p.stem: load_attack(p)
    for p in sorted((ROOT / "scripts" / "redteam" / "attacks").glob("*.toml"))
}


def test_the_committed_lab_files_are_what_the_generator_writes() -> None:
    for relative, wanted in lab_config.generated_files().items():
        path = ROOT / relative
        assert path.is_file(), f"{relative} is missing: run scripts/generate_lab_config.py"
        assert path.read_text(encoding="utf-8") == wanted, relative


def test_no_stray_file_sits_in_the_generated_directories() -> None:
    expected = {ROOT / relative for relative in lab_config.generated_files()}
    for directory in (LAB / "pipelines",):
        assert set(directory.glob("*.toml")) <= expected


# -- the columns --------------------------------------------------------------------------------


def test_the_columns_are_the_ones_the_scorecard_promises() -> None:
    ids = [c.id for c in lab_config.COLUMNS]

    assert len(ids) == len(set(ids))
    assert ids[:2] == ["all-on", "all-off"]
    assert ids[-1] == "denying-approver"
    assert {f"off-{layer}" for layer in LAYERS} <= set(ids)
    assert {"monitor", "denying-approver"} <= set(ids)
    assert {
        "before-all-flags",
        "before-short-text",
        "before-overlap",
        "before-validate-results",
    } <= set(ids)
    assert len(ids) == 17


@pytest.mark.parametrize("column", lab_config.COLUMNS, ids=lambda c: c.id)
def test_every_column_pipeline_loads_and_says_what_the_column_says(
    column: lab_config.Column,
) -> None:
    raw = tomllib.loads((LAB / "pipelines" / f"{column.id}.toml").read_text(encoding="utf-8"))

    config = parse_pipeline_config(raw, LAYER_ORDER)

    assert {name: mode.value for name, mode in config.modes.items()} == dict(column.modes)
    assert config.allow_floor_override is column.floor_override
    assert config.validate_results is column.validate_results
    assert set(column.modes) == LAYERS


def test_the_rejecting_approver_runs_last_so_its_standing_rejections_reach_no_other_column() -> (
    None
):
    """The gateway keeps a rejection for a client and an argument hash for 30 minutes, and every
    column reuses the same clients with the same writes: a rejection made in this column would
    block the same write in any column after it."""
    assert lab_config.COLUMNS[-1].id == "denying-approver"
    assert [c.id for c in lab_config.COLUMNS if c.approver == "reject"] == ["denying-approver"]


def test_a_floor_layer_is_weakened_only_in_a_column_that_says_it_is_a_lab_only_override() -> None:
    for column in lab_config.COLUMNS:
        weakened_floor = [
            layer
            for layer in ("scope", "approval")
            if column.modes[layer] != LayerMode.ENFORCE.value
        ]
        assert column.floor_override is bool(weakened_floor), column.id
        assert ("lab-only" in column.label) is bool(weakened_floor), column.id


def test_each_column_weakens_what_its_name_says_and_nothing_else() -> None:
    by_id = {c.id: c for c in lab_config.COLUMNS}

    assert set(by_id["all-on"].modes.values()) == {"enforce"}
    assert set(by_id["all-off"].modes.values()) == {"off"}
    for layer in LAYERS:
        off = by_id[f"off-{layer}"].modes
        assert off[layer] == "off"
        assert {m for name, m in off.items() if name != layer} == {"enforce"}
    monitor = by_id["monitor"].modes
    assert monitor["scope"] == monitor["approval"] == "enforce"
    assert {m for name, m in monitor.items() if name not in ("scope", "approval")} == {"monitor"}
    assert by_id["denying-approver"].approver == "reject"
    assert {c.approver for c in lab_config.COLUMNS if c.id != "denying-approver"} == {"approve"}


def test_the_before_columns_turn_back_exactly_the_flags_they_name() -> None:
    by_id = {c.id: c for c in lab_config.COLUMNS}

    assert by_id["all-on"].classifier == "current"
    assert by_id["all-on"].validate_results is True
    assert by_id["before-all-flags"].classifier == "legacy-both"
    assert by_id["before-all-flags"].validate_results is False
    assert (by_id["before-short-text"].classifier, by_id["before-short-text"].validate_results) == (
        "legacy-short-text",
        True,
    )
    assert (by_id["before-overlap"].classifier, by_id["before-overlap"].validate_results) == (
        "legacy-overlap",
        True,
    )
    assert (
        by_id["before-validate-results"].classifier,
        by_id["before-validate-results"].validate_results,
    ) == (
        "current",
        False,
    )
    for column in by_id.values():
        assert set(column.modes.values()) == {"enforce"} or not column.id.startswith("before-")


def test_the_classifier_variants_change_only_the_flags_and_load() -> None:
    product = load_judge_config(ROOT / "config" / "classifier.toml")
    legacy: dict[str, dict[str, Any]] = {
        "legacy-short-text": {"short_text": "legacy"},
        "legacy-overlap": {"unit_overlap_chars": 0},
        "legacy-both": {"short_text": "legacy", "unit_overlap_chars": 0},
    }
    for name, changes in legacy.items():
        loaded = load_judge_config(LAB / f"classifier.{name}.toml")
        assert loaded == dataclasses.replace(product, **changes), name


# -- a client for each attack ------------------------------------------------------------------


def test_every_attack_has_its_own_client_named_for_the_original_and_the_attack() -> None:
    clones = lab_config.clones()

    assert {c.attack for c in clones} == set(ATTACKS)
    assert len({c.name for c in clones}) == len(clones)
    for clone in clones:
        assert re.fullmatch(r"[a-z][a-z0-9-]{1,62}", clone.name), clone.name
        assert clone.name == f"{clone.original}--{clone.attack}"
        assert clone.original == ATTACKS[clone.attack].client


def test_a_clone_holds_exactly_the_scopes_of_its_original() -> None:
    from ai_gateway.admin.cli import DEMO_CLIENTS

    for clone in lab_config.clones():
        if clone.original == "harborline-lab-bot":
            assert set(clone.scopes) == set(lab_config.LAB_BOT_SCOPES)
        else:
            assert set(clone.scopes) == set(DEMO_CLIENTS[clone.original][1]), clone.name


def test_the_lab_allowlist_is_the_products_plus_each_rule_copied_for_each_clone_by_name() -> None:
    product = load_allowlist(ROOT / "config" / "allowlist.toml")
    lab = load_allowlist(LAB / "allowlist.lab.toml")
    clones = lab_config.clones()

    assert lab[: len(product)] == product, "the product's rules, unchanged and first"
    copies = lab[len(product) :]
    expected = [
        (rule, clone) for clone in clones for rule in product if rule.client == clone.original
    ]
    assert len(copies) == len(expected)
    for copy, (rule, clone) in zip(copies, expected, strict=True):
        assert copy.client == clone.name, "an exact client name, never a pattern"
        assert dataclasses.replace(copy, name=rule.name, client=rule.client) == rule
        assert copy.name == f"{rule.name}--{clone.name}"
    assert not [r for r in copies if "*" in r.client]


def test_the_rate_limit_and_role_files_have_no_per_client_entry_so_the_lab_uses_the_products() -> (
    None
):
    """Buckets are per client and roles are per tool: a clone has its own buckets and needs no
    entry. If either format ever gains a client key, this fails and the generator must copy it."""
    limits = load_rate_limits(ROOT / "config" / "rate_limits.toml")
    raw = tomllib.loads((ROOT / "config" / "rate_limits.toml").read_text(encoding="utf-8"))

    assert set(raw) <= {"reads", "writes", "tools"}
    assert all(set(v) == {"burst", "per"} for v in raw["tools"].values())
    assert limits.reads is not None
    roles = tomllib.loads((ROOT / "config" / "approval_roles.toml").read_text(encoding="utf-8"))
    assert set(roles) == {"roles_by_action"}
    assert all(re.fullmatch(r"[a-z]+__[a-z_]+", tool) for tool in roles["roles_by_action"])


# Clients an allowlist rule could be mistaken for matching if the production code ever matched
# names by pattern, prefix or substring instead of exactly. A rule is built directly, without the
# loader (which refuses most of these), so the matching itself is what is tested.
_RULE_CLIENTS = [
    "harborline-ops-bot",
    "harborline-*",
    "harborline-ops-bot*",
    "harborline-?ps-bot",
    "harborline-[a-z]ps-bot",
    "harborline-.*",
    "harborline-ops-bot--.*",
    "(harborline-ops-bot)|(harborline-support-bot)",
]
_CALLERS = [
    "harborline-ops-bot",
    "harborline-ops-bot--export-every-customer",
    "harborline-ops-bot-2",
    "xharborline-ops-bot",
    "harborline-support-bot",
    "harborline-aps-bot",
    "harborline-ops-bot*",
    "harborline-*",
    "harborline-.*",
    "harborline-ops-bot--.*",
    "",
]


def _rule(client: str) -> Any:
    from ai_gateway.pipeline.layers.allowlist import AllowlistRule

    return AllowlistRule(
        name="r", client=client, tool="tickets__create_ticket", argument="priority", required=True
    )


@pytest.mark.parametrize("client", _RULE_CLIENTS)
def test_a_rule_covers_exactly_the_client_it_names_whatever_the_name_looks_like(
    client: str,
) -> None:
    """Fails if glob, regular-expression, prefix or substring matching of client names is ever added
    to the production allowlist: only the literal name (and the single `*`) may match."""
    rule = _rule(client)

    for caller in _CALLERS:
        assert rule.covers(caller, "tickets__create_ticket") == (caller == client), (client, caller)
        assert not rule.covers(caller, "tickets__add_comment")


def test_the_star_alone_covers_every_client_and_no_other_name_is_a_wildcard() -> None:
    assert all(_rule("*").covers(caller, "tickets__create_ticket") for caller in _CALLERS)
    assert not _rule("**").covers("harborline-ops-bot", "tickets__create_ticket")
    assert not _rule("harborline-ops-bot").covers("*", "tickets__create_ticket")


@pytest.mark.anyio
async def test_a_rule_for_an_original_does_not_reach_its_clone_but_the_clones_own_copy_does() -> (
    None
):
    """The reason the lab allowlist copies each rule by exact name: nothing in the product lets a
    rule for `harborline-ops-bot` bind `harborline-ops-bot--x`."""
    from uuid import uuid4

    from ai_gateway.pipeline.layers.allowlist import AllowlistLayer
    from ai_gateway.pipeline.types import ALLOW, CallContext, ClientIdentity, Deny, ToolCall

    def ctx(name: str) -> CallContext:
        return CallContext(
            request_id=uuid4(),
            client=ClientIdentity(id=uuid4(), name=name, scopes=frozenset()),
            session_id=None,
            protocol_version="2025-11-25",
        )

    call = ToolCall.create(
        "tickets__create_ticket", "tickets", "create_ticket", {}, "write"
    )  # the rule requires `priority`, which is missing
    clone = "harborline-ops-bot--x"
    original_only = AllowlistLayer([_rule("harborline-ops-bot")])
    with_copy = AllowlistLayer([_rule("harborline-ops-bot"), dataclasses.replace(_rule(clone))])

    assert isinstance(await original_only.before_call(ctx("harborline-ops-bot"), call), Deny)
    assert await original_only.before_call(ctx(clone), call) is ALLOW
    assert isinstance(await with_copy.before_call(ctx(clone), call), Deny)


@pytest.mark.parametrize(
    "client",
    ["harborline-*", "harborline-?ps-bot", "harborline-[a-z]ps-bot", "harborline-.*", "**"],
)
def test_the_loader_refuses_a_client_that_is_a_pattern(client: str, tmp_path: Path) -> None:
    from ai_gateway.pipeline.layers.allowlist import AllowlistError

    path = tmp_path / "allowlist.toml"
    path.write_text(
        f'[[rule]]\nname = "r"\nclient = "{client}"\ntool = "tickets__create_ticket"\n'
        'argument = "priority"\none_of = ["normal"]\n',
        encoding="utf-8",
    )

    with pytest.raises(AllowlistError, match="not a client name"):
        load_allowlist(path)


def test_the_lab_allowlist_agrees_with_the_pins_the_lab_gateway_runs_with() -> None:
    """Every rule, the product's and each copy, names a tool the lab gateway has a pin for and an
    argument that tool's pinned input schema has; each clone is scoped to pinned tools only."""
    from ai_gateway.pipeline.pins import load_tool_pins

    pins = load_tool_pins(LAB / "tool_pins.lab.toml")
    product_pins = load_tool_pins(ROOT / "config" / "tool_pins.toml")
    lab = load_allowlist(LAB / "allowlist.lab.toml")

    assert pins.names() >= product_pins.names(), "the lab pins hold the product's, and more"
    for name in product_pins.names():
        assert pins.get(name) == product_pins.get(name), f"{name}: the lab pin differs"
    assert lab
    for rule in lab:
        pin = pins.get(rule.tool)
        assert pin is not None, f"{rule.name}: no pin for {rule.tool}"
        assert rule.argument in pin.input_schema.get("properties", {}), (
            f"{rule.name}: {rule.tool} has no argument {rule.argument!r}"
        )
    # The lab client may call every tool the lab upstream can offer in any phase. The only ones
    # with no pin are the tools it offers in the poisoned phase alone: unreviewed on purpose, so the
    # pinned-descriptions layer has something to hide. Any other unpinned scope is a mistake.
    from lab_upstream.tools import definitions as lab_definitions

    reviewed = {f"lab__{t.name}" for t in lab_definitions("reviewed")}
    unreviewed = {f"lab__{t.name}" for t in lab_definitions("poisoned")} - reviewed
    assert unreviewed
    for clone in lab_config.clones():
        unpinned = set(clone.scopes) - pins.names()
        assert unpinned <= unreviewed, f"{clone.name}: scoped to unpinned tools {sorted(unpinned)}"
    assert reviewed <= pins.names(), "every reviewed lab tool is pinned"
    assert not unreviewed & pins.names(), "no unreviewed lab tool is pinned"
