"""The schema layer: a call's arguments must fit the tool's reviewed input schema.

Detection: the arguments are validated (JSON Schema, draft 2020-12) against the *pinned* schema,
or, for a tool with no pin, the schema the catalog offers now. Beyond the schema itself the layer
refuses what no honest client sends: an argument the schema does not name (a top-level
`additionalProperties: false` is forced, whatever the server's schema says), a NUL character in any
key or string (the approval queue and the database refuse it later, and a person should not be
asked to approve it), a number that is not finite, nesting deeper than 6, and arguments over
64 KiB. It runs before approval, so a malformed write never reaches a person.

False positives: a client that sends `"5"` for an integer, or an extra argument the server used to
ignore. The servers validate strictly too (-32602); this refuses it before anything is asked or
forwarded. Monitor mode records `would_block` with the number of violations (`score`, at most 50)
and never the argument names or values.
"""

import itertools
import math
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry
from referencing.exceptions import Unresolvable

from ai_gateway.pipeline.pins import ToolPin, ToolPins
from ai_gateway.pipeline.types import (
    ALLOW,
    POLICY_BLOCK_MESSAGE,
    BaseLayer,
    CallContext,
    Deny,
    DenyCode,
    ToolCall,
    Verdict,
)

MAX_ARGUMENT_BYTES = 65_536
MAX_DEPTH = 6
MAX_VIOLATIONS = 50


def _shape_violations(value: Any, depth: int = 0) -> int:
    """How many things in the value no honest client sends: NUL, a non-finite number, too deep."""
    if depth > MAX_DEPTH:
        return 1
    if isinstance(value, str):
        return int("\x00" in value)
    if isinstance(value, float):
        return int(not math.isfinite(value))
    if isinstance(value, dict):
        return sum(
            int("\x00" in key) + _shape_violations(item, depth + 1) for key, item in value.items()
        )
    if isinstance(value, list):
        return sum(_shape_violations(item, depth + 1) for item in value)
    return 0


class SchemaLayer(BaseLayer):
    name = "schema"

    def __init__(self, pins: ToolPins | None = None) -> None:
        self._pins = pins if pins is not None else ToolPins({})
        # One validator per pin, built (and the schema itself checked) at startup: a pinned schema
        # that is not a valid schema stops the gateway starting, and nothing is built per call.
        self._validators = {
            name: self._validator(self._pins.get(name)) for name in self._pins.names()
        }

    @staticmethod
    def _validator(pin: ToolPin | None) -> Draft202012Validator:
        schema = {**(pin.input_schema if pin else {}), "additionalProperties": False}
        if pin is not None:
            Draft202012Validator.check_schema(schema)
        # An empty registry with no way to fetch: a `$ref` that is not inside the schema cannot be
        # resolved, so it is never retrieved from the network (jsonschema's default would try).
        return Draft202012Validator(schema, registry=Registry())

    def _validator_for(self, call: ToolCall) -> Draft202012Validator | None:
        known = self._validators.get(call.exposed_name)
        if known is not None:
            return known
        if call.definition is None:
            return None
        # A tool with no pin (`pinned_descriptions` refuses it, but this layer runs first): checked
        # against the schema the catalog offers now, never fetching anything.
        try:
            return Draft202012Validator(
                {**call.definition.input_schema, "additionalProperties": False}, registry=Registry()
            )
        except Exception:  # an upstream's own schema is not trusted to be valid
            return None

    async def before_call(self, ctx: CallContext, call: ToolCall) -> Verdict:
        validator = self._validator_for(call)
        if validator is None:
            return Deny(DenyCode.SCHEMA_VIOLATION, POLICY_BLOCK_MESSAGE, score=1)
        if len(call.arguments_json.encode()) > MAX_ARGUMENT_BYTES:
            return Deny(DenyCode.SCHEMA_VIOLATION, POLICY_BLOCK_MESSAGE, score=1)
        arguments = call.arguments
        violations = _shape_violations(arguments)
        try:
            errors = validator.iter_errors(arguments)
            violations += sum(1 for _ in itertools.islice(errors, MAX_VIOLATIONS))
        except Unresolvable:
            violations += 1  # a reference to somewhere that cannot be reached is a refusal
        if violations:
            return Deny(
                DenyCode.SCHEMA_VIOLATION,
                POLICY_BLOCK_MESSAGE,
                score=min(violations, MAX_VIOLATIONS),
            )
        return ALLOW
