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


def test_production_code_has_no_client_name_matching_beyond_the_single_star() -> None:
    source = (
        ROOT / "gateway" / "src" / "ai_gateway" / "pipeline" / "layers" / "allowlist.py"
    ).read_text(encoding="utf-8")

    assert "fnmatch" not in source
    assert "glob" not in source.lower()
    assert 'client == "*"' in source or '"*"' in source
