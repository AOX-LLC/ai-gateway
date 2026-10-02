"""The input-schema contract every tool of an MCP server must meet.

An input schema is the first wall against a hostile or confused model, so each tool must
describe a closed, bounded shape: no unknown properties, no unbounded strings, integers
or arrays, and no nested objects whose contents would escape these rules. Tests run this
over each server's tools and over the gateway's catalog of them.
"""

from collections.abc import Mapping
from typing import Any

_COMBINATORS = ("anyOf", "oneOf", "allOf")


def check_input_schema(schema: Mapping[str, Any]) -> list[str]:
    """Return one message per violation; an empty list means the schema meets the contract."""
    problems: list[str] = []
    if schema.get("type") != "object":
        problems.append("root: must be an object schema")
    _check_object(schema, "root", problems)
    return problems


def _check_object(schema: Mapping[str, Any], path: str, problems: list[str]) -> None:
    if schema.get("additionalProperties") is not False:
        problems.append(f"{path}: additionalProperties must be false")
    if "$ref" in schema or "$defs" in schema:
        problems.append(f"{path}: $ref/$defs are not allowed; inline the types")
    for name, subschema in schema.get("properties", {}).items():
        _check_property(subschema, f"{path}.{name}", problems)


def _check_property(schema: Mapping[str, Any], path: str, problems: list[str]) -> None:
    for combinator in _COMBINATORS:
        for branch in schema.get(combinator, []):
            _check_property(branch, path, problems)
    if "$ref" in schema:
        problems.append(f"{path}: $ref is not allowed; inline the type")

    kind = schema.get("type")
    kinds = kind if isinstance(kind, list) else [kind]
    if "string" in kinds and not _is_bounded_string(schema):
        problems.append(f"{path}: a string needs maxLength, enum or pattern")
    is_number = "integer" in kinds or "number" in kinds
    if is_number and ("minimum" not in schema or "maximum" not in schema):
        problems.append(f"{path}: a number needs minimum and maximum")
    if "array" in kinds and "maxItems" not in schema:
        problems.append(f"{path}: an array needs maxItems")
    if "object" in kinds:
        problems.append(f"{path}: nested objects are not allowed")


def _is_bounded_string(schema: Mapping[str, Any]) -> bool:
    return any(key in schema for key in ("maxLength", "enum", "pattern", "const"))
