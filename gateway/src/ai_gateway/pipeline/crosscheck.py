"""Check the policy files against the reviewed tool definitions at startup.

The allowlist, the rate limits and the approval roles all name tools and arguments. A name that no
tool has is a rule that never fires, and a typo in one is a control that silently does nothing. The
reviewed catalogue is the pin file (`config/tool_pins.toml`), which needs no upstream to be up, so
the check runs before the gateway accepts a request and stops startup on a mistake: a tool no pin
names, an argument the pinned schema does not have, or a rule that cannot apply to the argument's
type (a range on a string, a value outside an enum).
"""

from collections.abc import Collection, Mapping, Sequence
from typing import Any

from ai_gateway.pipeline.layers.allowlist import AllowlistRule
from ai_gateway.pipeline.layers.rate_limit import RateLimits
from ai_gateway.pipeline.pins import ToolPins


class ConfigCrossCheckError(ValueError):
    pass


_NUMERIC = {"integer", "number"}


def _types(schema: Mapping[str, Any]) -> set[str]:
    declared = schema.get("type")
    if isinstance(declared, str):
        return {declared}
    if isinstance(declared, list):
        return {item for item in declared if isinstance(item, str)}
    return set()


def check_policy_files(
    pins: ToolPins,
    *,
    allowlist: Sequence[AllowlistRule] = (),
    rate_limits: RateLimits | None = None,
    approval_tools: Collection[str] = (),
) -> None:
    """Raise ConfigCrossCheckError, naming every mistake, or return."""
    problems: list[str] = []
    known = pins.names()
    for rule in allowlist:
        pin = pins.get(rule.tool)
        if pin is None:
            problems.append(f"allowlist rule {rule.name!r}: no tool {rule.tool!r} is pinned")
            continue
        properties = pin.input_schema.get("properties", {})
        if rule.argument not in properties:
            problems.append(
                f"allowlist rule {rule.name!r}: {rule.tool} has no argument {rule.argument!r}"
            )
            continue
        schema = properties[rule.argument]
        types = _types(schema)
        if (
            types
            and (rule.minimum is not None or rule.maximum is not None)
            and not types & _NUMERIC
        ):
            problems.append(f"allowlist rule {rule.name!r}: a range on a non-numeric argument")
        if (
            types
            and (rule.pattern is not None or rule.max_length is not None)
            and "string" not in types
        ):
            problems.append(f"allowlist rule {rule.name!r}: a pattern or length on a non-string")
        allowed = schema.get("enum")
        if rule.one_of is not None and isinstance(allowed, list):
            outside = [value for value in rule.one_of if value not in allowed]
            if outside:
                problems.append(
                    f"allowlist rule {rule.name!r}: one_of holds values the schema's enum does not"
                )
    for tool in (rate_limits.tools or {}) if rate_limits else {}:
        if tool not in known:
            problems.append(f"rate limit for {tool!r}: no such tool is pinned")
    for tool in approval_tools:
        if tool not in known:
            problems.append(f"approval role for {tool!r}: no such tool is pinned")
    if problems:
        raise ConfigCrossCheckError(
            "the policy files do not match the pinned tools: " + "; ".join(problems)
        )
