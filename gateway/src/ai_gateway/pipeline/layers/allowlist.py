r"""The allowlist layer: per-client rules on the values of a tool's arguments.

Scope decides which tools a client may call; approval puts a person in front of a write. This layer
decides what a client may *say* to a tool, for the cases where a person's yes should not be enough
or should not be needed: a support bot may open a ticket at any priority but `urgent`, an ops bot
may not set a status the workflow forbids. Rules are value constraints only: enumerated values, a
pattern the whole value must match, a length, a numeric range. They never rewrite an argument.

    [[rule]]
    name = "support-bot-no-urgent-tickets"
    client = "harborline-support-bot"     # a client's name, or "*" for every client
    tool = "tickets__create_ticket"       # the exposed name
    argument = "priority"                 # a top-level argument
    one_of = ["low", "normal", "high"]    # and/or pattern, max_length, minimum, maximum
    required = false                      # true: the argument must be present

A pattern is compiled in ASCII mode: `\d` is 0-9 and nothing else, and case folding does not reach
beyond ASCII. A value that is NaN or infinite fails any numeric rule.

A call to a tool with no rule is not constrained by this layer. When several rules cover a call,
every one must pass. A mistake in the file stops the gateway from starting.
"""

import logging
import math
import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ai_gateway.pipeline.types import (
    ALLOW,
    BaseLayer,
    CallContext,
    Deny,
    DenyCode,
    ToolCall,
    Verdict,
)

logger = logging.getLogger(__name__)

POLICY_BLOCK_MESSAGE = "Request blocked by gateway policy."
_CLIENT_NAME = re.compile(r"[a-z][a-z0-9-]{1,62}")
_EXPOSED_TOOL = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*__[a-z][a-z0-9_]*")
_RULE_KEYS = frozenset(
    {
        "name",
        "client",
        "tool",
        "argument",
        "one_of",
        "pattern",
        "max_length",
        "minimum",
        "maximum",
        "required",
    }
)
_MAX_MATCHED_LENGTH = 4096
"""A value longer than this is not matched against a pattern: it fails, whatever the pattern says.
Patterns come from a reviewed file, but the value is the client's."""


class AllowlistError(ValueError):
    pass


@dataclass(frozen=True)
class AllowlistRule:
    name: str
    client: str
    tool: str
    argument: str
    one_of: tuple[Any, ...] | None = None
    pattern: re.Pattern[str] | None = None
    max_length: int | None = None
    minimum: float | None = None
    maximum: float | None = None
    required: bool = False

    def covers(self, client_name: str, tool: str) -> bool:
        return self.tool == tool and self.client in ("*", client_name)

    def violated_by(self, arguments: Mapping[str, Any]) -> bool:
        if self.argument not in arguments:
            return self.required
        value = arguments[self.argument]
        if self.one_of is not None and not any(
            type(value) is type(allowed) and value == allowed for allowed in self.one_of
        ):
            return True
        if self.pattern is not None and not (
            isinstance(value, str)
            and len(value) <= _MAX_MATCHED_LENGTH
            and self.pattern.fullmatch(value)
        ):
            return True
        if self.max_length is not None and not (
            isinstance(value, str) and len(value) <= self.max_length
        ):
            return True
        if self.minimum is not None or self.maximum is not None:
            if isinstance(value, bool) or not isinstance(value, int | float):
                return True
            if isinstance(value, float) and not math.isfinite(value):
                return True  # NaN is neither below nor above anything: it would pass a range
            if (self.minimum is not None and value < self.minimum) or (
                self.maximum is not None and value > self.maximum
            ):
                return True
        return False


def load_allowlist(path: Path) -> list[AllowlistRule]:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise AllowlistError(f"cannot read the allowlist {path}: {error}") from error
    unknown = set(raw) - {"rule"}
    if unknown:
        raise AllowlistError(f"unknown keys in {path}: {sorted(unknown)}")
    entries = raw.get("rule", [])
    if not isinstance(entries, list):
        raise AllowlistError("[[rule]] must be an array of tables")
    rules = [_parse_rule(entry, number) for number, entry in enumerate(entries, start=1)]
    names = [rule.name for rule in rules]
    if len(set(names)) != len(names):
        raise AllowlistError("two rules have the same name")
    return rules


def _parse_rule(entry: object, number: int) -> AllowlistRule:
    where = f"rule {number}"
    if not isinstance(entry, dict):
        raise AllowlistError(f"{where} must be a table")
    unknown = set(entry) - _RULE_KEYS
    if unknown:
        raise AllowlistError(f"{where} has unknown keys {sorted(unknown)}")
    for key in ("name", "client", "tool", "argument"):
        if not isinstance(entry.get(key), str) or not entry[key]:
            raise AllowlistError(f"{where} needs a non-empty string {key!r}")
    where = f"rule {entry['name']!r}"
    if entry["client"] != "*" and not _CLIENT_NAME.fullmatch(entry["client"]):
        raise AllowlistError(f"{where}: client {entry['client']!r} is not a client name or '*'")
    if not _EXPOSED_TOOL.fullmatch(entry["tool"]):
        raise AllowlistError(f"{where}: tool {entry['tool']!r} is not a <namespace>__<tool> name")
    one_of = entry.get("one_of")
    if one_of is not None and (not isinstance(one_of, list) or not one_of):
        raise AllowlistError(f"{where}: one_of must be a non-empty list")
    pattern = None
    if "pattern" in entry:
        if not isinstance(entry["pattern"], str):
            raise AllowlistError(f"{where}: pattern must be a string")
        try:
            pattern = re.compile(entry["pattern"], re.ASCII)
        except re.error as error:
            raise AllowlistError(f"{where}: pattern does not compile: {error}") from error
    max_length = entry.get("max_length")
    if max_length is not None and (
        isinstance(max_length, bool) or not isinstance(max_length, int) or max_length < 0
    ):
        raise AllowlistError(f"{where}: max_length must be a non-negative integer")
    bounds: dict[str, float | None] = {}
    for key in ("minimum", "maximum"):
        value = entry.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int | float)):
            raise AllowlistError(f"{where}: {key} must be a number")
        bounds[key] = value
    required = entry.get("required", False)
    if not isinstance(required, bool):
        raise AllowlistError(f"{where}: required must be true or false")
    constrained = (
        one_of is not None
        or pattern is not None
        or max_length is not None
        or bounds["minimum"] is not None
        or bounds["maximum"] is not None
        or required
    )
    if not constrained:
        raise AllowlistError(f"{where} constrains nothing")
    return AllowlistRule(
        name=entry["name"],
        client=entry["client"],
        tool=entry["tool"],
        argument=entry["argument"],
        one_of=tuple(one_of) if one_of is not None else None,
        pattern=pattern,
        max_length=max_length,
        minimum=bounds["minimum"],
        maximum=bounds["maximum"],
        required=required,
    )


class AllowlistLayer(BaseLayer):
    name = "allowlist"

    def __init__(self, rules: Sequence[AllowlistRule] = ()) -> None:
        self._rules = tuple(rules)

    async def before_call(self, ctx: CallContext, call: ToolCall) -> Verdict:
        arguments = call.arguments
        for rule in self._rules:
            if rule.covers(ctx.client.name, call.exposed_name) and rule.violated_by(arguments):
                # Which rule is for the operator's log, not the client. Never the value.
                logger.warning(
                    "allowlist rule %r refused request %s from %s",
                    rule.name,
                    ctx.request_id,
                    ctx.client.name,
                )
                return Deny(DenyCode.ALLOWLIST_VIOLATION, POLICY_BLOCK_MESSAGE)
        return ALLOW
