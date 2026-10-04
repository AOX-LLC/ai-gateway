"""The policy files are checked against the pinned tools at startup."""

import dataclasses
from pathlib import Path
from typing import Any

import pytest

from ai_gateway.pipeline.crosscheck import ConfigCrossCheckError, check_policy_files
from ai_gateway.pipeline.layers.allowlist import AllowlistRule, load_allowlist
from ai_gateway.pipeline.layers.rate_limit import Limit, RateLimits, load_rate_limits
from ai_gateway.pipeline.pins import load_tool_pins
from ai_gateway.policy.roles import load_roles_by_action

ROOT = Path(__file__).resolve().parents[2]
PINS = load_tool_pins(ROOT / "config" / "tool_pins.toml")


def _rule(**changes: Any) -> AllowlistRule:
    base = AllowlistRule(
        name="r",
        client="*",
        tool="tickets__create_ticket",
        argument="priority",
        one_of=("low", "high"),
    )
    return dataclasses.replace(base, **changes)


def test_the_shipped_policy_files_match_the_pinned_tools() -> None:
    check_policy_files(
        PINS,
        allowlist=load_allowlist(ROOT / "config" / "allowlist.toml"),
        rate_limits=load_rate_limits(ROOT / "config" / "rate_limits.toml"),
        approval_tools=load_roles_by_action(ROOT / "config" / "approval_roles.toml"),
    )


@pytest.mark.parametrize(
    ("rule", "message"),
    [
        (_rule(tool="tickets__create_tickets"), "no tool 'tickets__create_tickets' is pinned"),
        (_rule(argument="prioritee"), "no argument 'prioritee'"),
        (_rule(one_of=("low", "emergency")), "enum does not"),
        (_rule(one_of=None, minimum=1.0), "non-numeric"),
        (
            _rule(
                one_of=None,
                argument="limit",
                tool="tickets__list_tickets",
                pattern=None,
                max_length=3,
            ),
            "non-string",
        ),
    ],
    ids=[
        "unknown-tool",
        "unknown-argument",
        "outside-enum",
        "range-on-string",
        "length-on-integer",
    ],
)
def test_an_unknown_tool_or_argument_or_a_rule_that_cannot_apply_stops_startup(
    rule: AllowlistRule, message: str
) -> None:
    with pytest.raises(ConfigCrossCheckError, match=message):
        check_policy_files(PINS, allowlist=[rule])


def test_a_rate_limit_or_approval_role_for_an_unknown_tool_stops_startup() -> None:
    limit = Limit(burst=1, per=60)
    with pytest.raises(ConfigCrossCheckError, match="rate limit for 'crm__list_dealz'"):
        check_policy_files(PINS, rate_limits=RateLimits(tools={"crm__list_dealz": limit}))
    with pytest.raises(ConfigCrossCheckError, match="approval role for 'tickets__asign'"):
        check_policy_files(PINS, approval_tools=["tickets__asign"])


def test_every_mistake_is_named_not_just_the_first() -> None:
    with pytest.raises(ConfigCrossCheckError) as stopped:
        check_policy_files(
            PINS, allowlist=[_rule(tool="x__y"), _rule(argument="nope")], approval_tools=["z__w"]
        )

    assert str(stopped.value).count(";") == 2


def test_the_gateway_does_not_start_when_the_allowlist_names_a_tool_nobody_has(
    tmp_path: Path,
) -> None:
    from pydantic import SecretStr

    from ai_gateway.app import create_app
    from ai_gateway.settings import GatewaySettings

    allowlist = tmp_path / "allowlist.toml"
    allowlist.write_text(
        '[[rule]]\nname = "typo"\nclient = "*"\ntool = "tickets__create_ticket"\n'
        'argument = "prioritee"\none_of = ["low"]\n'
    )
    settings = GatewaySettings(
        database_url=SecretStr("postgresql://x:y@127.0.0.1:1/z"),
        pipeline_file=ROOT / "config" / "pipeline.toml",
        allowlist_file=allowlist,
        rate_limits_file=ROOT / "config" / "rate_limits.toml",
        tool_pins_file=ROOT / "config" / "tool_pins.toml",
        egress_file=ROOT / "config" / "egress.toml",
        canaries_file=ROOT / "config" / "canaries.toml",
    )

    with pytest.raises(ConfigCrossCheckError, match="no argument 'prioritee'"):
        create_app(settings)
