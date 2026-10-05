"""The lab gateway's policy files: the product's plus the lab upstream's, matching its pins."""

import tomllib
from pathlib import Path

from ai_gateway.pipeline.crosscheck import check_policy_files
from ai_gateway.pipeline.layers.allowlist import load_allowlist
from ai_gateway.pipeline.layers.rate_limit import load_rate_limits
from ai_gateway.pipeline.pins import load_tool_pins
from ai_gateway.policy.roles import load_roles_by_action
from ai_gateway.registry.tool_policies import load_tool_policies
from lab_upstream.tools import REVIEWED_TOOLS, definitions

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / "config" / "lab"


def test_the_lab_approval_roles_are_the_products_plus_the_lab_write() -> None:
    product = dict(load_roles_by_action(ROOT / "config" / "approval_roles.toml"))
    lab = dict(load_roles_by_action(LAB / "approval_roles.lab.toml"))

    assert lab == {**product, "lab__forward_note": "approver"}
    text = (ROOT / "config" / "approval_roles.toml").read_text(encoding="utf-8").rstrip("\n")
    assert (LAB / "approval_roles.lab.toml").read_text(encoding="utf-8").count(text) == 1


def test_every_lab_tool_has_a_policy_and_only_the_one_write_is_a_write() -> None:
    policies = {
        (p.namespace, p.tool): p.effect for p in load_tool_policies(LAB / "tool_policies.lab.toml")
    }
    everything = {tool.name for tool in definitions("poisoned")}

    assert {tool for namespace, tool in policies if namespace == "lab"} == everything
    assert {tool for (_, tool), effect in policies.items() if effect == "write"} == {"forward_note"}
    assert set(REVIEWED_TOOLS) <= everything


def test_the_lab_policy_files_match_the_lab_pins_at_startup() -> None:
    check_policy_files(
        load_tool_pins(LAB / "tool_pins.lab.toml"),
        allowlist=load_allowlist(ROOT / "config" / "allowlist.toml"),
        rate_limits=load_rate_limits(ROOT / "config" / "rate_limits.toml"),
        approval_tools=load_roles_by_action(LAB / "approval_roles.lab.toml"),
    )


def test_the_product_policy_files_are_not_touched_by_the_lab() -> None:
    policies = tomllib.loads((ROOT / "config" / "tool_policies.toml").read_text(encoding="utf-8"))

    assert "lab" not in policies
    roles = load_roles_by_action(ROOT / "config" / "approval_roles.toml")
    assert not [name for name in roles if name.startswith("lab__")]
