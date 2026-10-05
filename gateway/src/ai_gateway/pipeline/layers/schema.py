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
*pinned* output schema (`unevaluatedProperties: false` is forced at the root, so a field the review
never saw cannot ride along, whatever shape the root has, and the same for every nested object;
a result with `_meta` is refused). A result with no structured content where the pin
has an output schema is refused too, as is an error-free result that fails the schema. The text
blocks are a second channel a client may be shown, so each must be a bare text block that says what
the structured content says (its JSON, or, for FastMCP's `{"result": value}` wrapper, the value
itself): any other block, and any text that says more or other, is refused. A tool error
(`isError`) must be plain text, with no structured content and no other kind of block (its words
are the one thing not checked here; the classifier judges a read's error text), and a tool whose pin
has no output schema is not checked: it has nothing reviewed to compare with. The text is read the
strictest way (no `NaN`, no repeated key) and compared as JSON, so `true` is not `1`. It is
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


_SCHEMA_MAPS = ("properties", "patternProperties", "$defs", "definitions", "dependentSchemas")
_SCHEMA_LISTS = ("allOf", "anyOf", "prefixItems")
_SCHEMAS = ("items", "additionalProperties")


def _closed(schema: Any, *, root: bool = False) -> Any:
    """The schema with every object in it closed: a field it does not name is a violation.
    `unevaluatedProperties: false` sees through `$ref`, `allOf` and `anyOf`, so the root and each
    nested object (a pydantic model's nested models name their fields but do not forbid others)
    refuse an extra field whatever shape they have. A map (`additionalProperties` is a schema, no
    `properties`) keeps its open keys: its values are closed instead. Only keywords where a closed
    subschema can match less and so refuse more are walked: `not`, `if`, `contains` and `oneOf` are
    left as written, since closing inside them would flip their meaning (a closed `not` matches
    less, so more passes); the root's `unevaluatedProperties` still covers what they let through."""
    if not isinstance(schema, dict):
        return schema
    closed = dict(schema)
    for key in _SCHEMA_MAPS:
        if isinstance(closed.get(key), dict):
            closed[key] = {name: _closed(sub) for name, sub in closed[key].items()}
    for key in _SCHEMA_LISTS:
        if isinstance(closed.get(key), list):
            closed[key] = [_closed(sub) for sub in closed[key]]
    for key in _SCHEMAS:
        if key in closed:
            closed[key] = _closed(closed[key])
    is_map = isinstance(closed.get("additionalProperties"), dict) and "properties" not in closed
    names_fields = "properties" in closed or closed.get("type") == "object" or root
    if names_fields and not is_map:
        if "additionalProperties" in closed and not isinstance(
            closed["additionalProperties"], dict
        ):
            closed["additionalProperties"] = False
        closed["unevaluatedProperties"] = False
    return closed


def _strict_json(text: str) -> Any:
    """The text parsed the strictest way any client might: no NaN or Infinity, no repeated key
    (parsers disagree on which one wins), and a ValueError, never a crash, for anything else."""

    def no_repeats(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        keys = [key for key, _ in pairs]
        if len(set(keys)) != len(keys):
            raise ValueError("a key is repeated")
        return dict(pairs)

    def no_constants(name: str) -> None:
        raise ValueError(f"{name} is not JSON")

    try:
        return json.loads(text, object_pairs_hook=no_repeats, parse_constant=no_constants)
    except RecursionError as error:
        raise ValueError("nested too deep") from error


def _canonical(value: Any) -> str:
    """The value as JSON with sorted keys. Unlike `==`, it tells `true` from `1` and `1.0` from
    `1`, which Python's equality does not and another parser would."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _says(text: str, value: Any) -> bool:
    """Whether a text block says exactly this value: the string itself for a string, otherwise its
    JSON (in any layout, compared as JSON, so `true` is not `1`)."""
    if isinstance(value, str):
        return text == value
    try:
        return _canonical(_strict_json(text)) == _canonical(value)
    except ValueError:
        return False


def _blocks_repeat(result: CallToolResult, structured: Any) -> bool:
    """Whether the content blocks say nothing the structured content does not: what a client is
    shown in the text is then what was validated, and nothing rides along beside it. Every block
    is a bare text block, and the texts are either each the JSON of the structured content, or,
    for the `{"result": value}` wrapper the Python SDK's FastMCP puts around a plain return value,
    the value itself: one block for a string or a number, one per item for a list."""
    if any(
        not isinstance(block, TextContent) or block.meta or block.annotations
        for block in result.content
    ):
        return False
    texts = [block.text for block in result.content if isinstance(block, TextContent)]
    if all(_says(text, structured) for text in texts):
        return True
    if isinstance(structured, dict) and set(structured) == {"result"}:
        value = structured["result"]
        items = value if isinstance(value, list) else [value]
        return len(texts) == len(items) and all(
            _says(text, item) for text, item in zip(texts, items, strict=True)
        )
    return False


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
        schema = _closed(output_schema, root=True)
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
        if validator is None:
            return ALLOW
        if result.is_error:
            # A tool error is plain text: data or another kind of block does not ride on one.
            plain = (
                not result.meta
                and result.structured_content is None
                and all(isinstance(block, TextContent) for block in result.content)
            )
            return (
                ALLOW if plain else Deny(DenyCode.SCHEMA_VIOLATION, POLICY_BLOCK_MESSAGE, score=1)
            )
        content = result.structured_content
        if content is None:
            return Deny(DenyCode.SCHEMA_VIOLATION, POLICY_BLOCK_MESSAGE, score=1)
        if result.meta or not _blocks_repeat(result, content):
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
