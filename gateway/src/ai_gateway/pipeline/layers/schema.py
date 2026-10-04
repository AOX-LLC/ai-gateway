"""The schema layer: a call's arguments, and the structured content of its result, must fit the
tool's reviewed schemas.

Detection: the arguments are validated (JSON Schema, draft 2020-12) against the *pinned* schema,
or, for a tool with no pin, the schema the catalog offers now. Beyond the schema itself the layer
refuses what no honest client sends: an argument the schema does not name (a top-level
`additionalProperties: false` is forced, whatever the server's schema says), a NUL character in any
key or string (the approval queue and the database refuse it later, and a person should not be
asked to approve it), a number that is not finite, nesting deeper than 6, and arguments over
64 KiB. It runs before approval, so a malformed write never reaches a person.

Results: after the upstream answers, a result's structured content is validated against the
*pinned* output schema (a top-level `additionalProperties: false` is forced on an object schema, so
a field the review never saw cannot ride along). A result with no structured content where the pin
has an output schema is refused too, as is an error-free result that fails the schema. The text
blocks are a second channel a client may be shown, so each must be a text block whose JSON is the
structured content itself: any other block, any text that is not that JSON, is refused. A tool error
(`isError`) and a tool whose pin has no output schema are not checked: the second has nothing
reviewed to compare with. Nested objects are checked only as far as the schema itself says. It is
on unless `[schema] validate_results = false` in the pipeline file (v0.1.0 did not check results).

False positives: a client that sends `"5"` for an integer, or an extra argument the server used to
ignore. The servers validate strictly too (-32602); this refuses it before anything is asked or
forwarded. Monitor mode records `would_block` with the number of violations (`score`, at most 50)
and never the argument names or values.
"""

import itertools
import json
import math
from typing import Any

from jsonschema import Draft202012Validator
from mcp.types import CallToolResult, TextContent
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


def _blocks_repeat(result: CallToolResult, structured: Any) -> bool:
    """Whether every content block is a text block whose JSON is the structured content: what a
    client is shown in the text is then what was validated, and nothing rides along beside it."""
    for block in result.content:
        if not isinstance(block, TextContent):
            return False
        try:
            if json.loads(block.text) != structured:
                return False
        except ValueError:
            return False
    return True


class SchemaLayer(BaseLayer):
    name = "schema"

    def __init__(self, pins: ToolPins | None = None, validate_results: bool = False) -> None:
        self._pins = pins if pins is not None else ToolPins({})
        self._validate_results = validate_results
        # One validator per pin, built (and the schema itself checked) at startup: a pinned schema
        # that is not a valid schema stops the gateway starting, and nothing is built per call.
        self._validators = {
            name: self._validator(self._pins.get(name)) for name in self._pins.names()
        }
        self._result_validators = {
            name: self._result_validator(pin.output_schema)
            for name in self._pins.names()
            if validate_results and (pin := self._pins.get(name)) and pin.output_schema is not None
        }

    @staticmethod
    def _validator(pin: ToolPin | None) -> Draft202012Validator:
        schema = {**(pin.input_schema if pin else {}), "additionalProperties": False}
        if pin is not None:
            Draft202012Validator.check_schema(schema)
        # An empty registry with no way to fetch: a `$ref` that is not inside the schema cannot be
        # resolved, so it is never retrieved from the network (jsonschema's default would try).
        return Draft202012Validator(schema, registry=Registry())

    @staticmethod
    def _result_validator(output_schema: Any) -> Draft202012Validator:
        schema = dict(output_schema)
        if schema.get("type") == "object":
            schema["additionalProperties"] = False
        Draft202012Validator.check_schema(schema)
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

    async def after_call(self, ctx: CallContext, call: ToolCall, result: CallToolResult) -> Verdict:
        validator = self._result_validators.get(call.exposed_name)
        if validator is None or result.is_error:
            return ALLOW
        content = result.structured_content
        if content is None:
            return Deny(DenyCode.SCHEMA_VIOLATION, POLICY_BLOCK_MESSAGE, score=1)
        if not _blocks_repeat(result, content):
            return Deny(DenyCode.SCHEMA_VIOLATION, POLICY_BLOCK_MESSAGE, score=1)
        violations = _shape_violations(content)
        try:
            errors = validator.iter_errors(content)
            violations += sum(1 for _ in itertools.islice(errors, MAX_VIOLATIONS))
        except Unresolvable:
            violations += 1
        if violations:
            return Deny(
                DenyCode.SCHEMA_VIOLATION,
                POLICY_BLOCK_MESSAGE,
                score=min(violations, MAX_VIOLATIONS),
            )
        return ALLOW
