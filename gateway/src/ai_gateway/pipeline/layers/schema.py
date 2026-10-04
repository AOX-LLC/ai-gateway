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

from ai_gateway.pipeline.pins import ToolPins
from ai_gateway.pipeline.types import (
    ALLOW,
    BaseLayer,
    CallContext,
    Deny,
    DenyCode,
    ToolCall,
    Verdict,
)

POLICY_BLOCK_MESSAGE = "Request blocked by gateway policy."
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

    def _schema_for(self, call: ToolCall) -> dict[str, Any] | None:
        pin = self._pins.get(call.exposed_name)
        if pin is not None:
            return dict(pin.input_schema)
        if call.definition is not None:
            return call.definition.input_schema
        return None

    async def before_call(self, ctx: CallContext, call: ToolCall) -> Verdict:
        schema = self._schema_for(call)
        if schema is None:
            return Deny(DenyCode.SCHEMA_VIOLATION, POLICY_BLOCK_MESSAGE, score=1)
        if len(call.arguments_json.encode()) > MAX_ARGUMENT_BYTES:
            return Deny(DenyCode.SCHEMA_VIOLATION, POLICY_BLOCK_MESSAGE, score=1)
        arguments = call.arguments
        violations = _shape_violations(arguments)
        strict = {**schema, "additionalProperties": False}
        errors = Draft202012Validator(strict).iter_errors(arguments)
        violations += sum(1 for _ in itertools.islice(errors, MAX_VIOLATIONS))
        if violations:
            return Deny(
                DenyCode.SCHEMA_VIOLATION,
                POLICY_BLOCK_MESSAGE,
                score=min(violations, MAX_VIOLATIONS),
            )
        return ALLOW
