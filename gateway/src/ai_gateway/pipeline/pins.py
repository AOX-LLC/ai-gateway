"""The reviewed definition of each tool: its description and input schema, pinned by hash.

A tool's description and schema are what a model reads when it decides what to call and with what,
so a server that changes them after review (a "rug pull") changes what the model is told. The
`pinned_descriptions` layer compares each tool the catalog offers with the pin a person reviewed,
and the `schema` layer validates arguments against the pinned schema.

The pins live in `config/tool_pins.toml`, in git, so a change to a description is a diff someone
reads:

    [tools."tickets__create_ticket"]
    sha256 = "..."                 # of the canonical JSON of the name, description and both schemas
    description = "Opens a ticket..."
    input_schema = '''{ ... }'''   # the schema as JSON, pretty-printed
    output_schema = '''{ ... }'''  # the output schema (or null), the same way

`gateway-admin`'s sibling script `scripts/generate_tool_pins.py` writes the file from the servers'
own definitions. The file is read once at startup (re-pinning means a restart) and a mistake stops
the gateway from starting: an entry whose hash is not the hash of its own text, an unknown key, a
schema that is not JSON.
"""

import hashlib
import json
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ToolPinsError(ValueError):
    pass


def definition_sha256(
    name: str,
    description: str | None,
    input_schema: Mapping[str, Any],
    output_schema: Mapping[str, Any] | None = None,
) -> str:
    """The hash a pin records: of the canonical JSON of the tool's exposed name, its description
    and its input and output schemas (keys sorted, no insignificant whitespace, UTF-8). The output
    schema is part of what a client is told about the tool, and its field descriptions are text a
    model reads, so a change to it is drift like a change to the description."""
    canonical = json.dumps(
        {
            "name": name,
            "description": description or "",
            "inputSchema": input_schema,
            "outputSchema": output_schema,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class ToolPin:
    description: str
    input_schema: Mapping[str, Any]
    sha256: str
    output_schema: Mapping[str, Any] | None = None


class ToolPins:
    def __init__(self, pins: Mapping[str, ToolPin]) -> None:
        self._pins = dict(pins)

    def get(self, exposed_name: str) -> ToolPin | None:
        return self._pins.get(exposed_name)

    def names(self) -> frozenset[str]:
        return frozenset(self._pins)

    def __len__(self) -> int:
        return len(self._pins)


_ENTRY_KEYS = frozenset({"sha256", "description", "input_schema", "output_schema"})


def load_tool_pins(path: Path) -> ToolPins:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ToolPinsError(f"cannot read the tool pins {path}: {error}") from error
    return parse_tool_pins(raw, str(path))


def parse_tool_pins(raw: Mapping[str, Any], where: str = "tool pins") -> ToolPins:
    unknown = set(raw) - {"tools"}
    if unknown:
        raise ToolPinsError(f"unknown keys in {where}: {sorted(unknown)}")
    tools = raw.get("tools", {})
    if not isinstance(tools, Mapping):
        raise ToolPinsError(f"[tools] in {where} must be a table")
    pins: dict[str, ToolPin] = {}
    for name, entry in tools.items():
        if not isinstance(entry, Mapping) or set(entry) != _ENTRY_KEYS:
            raise ToolPinsError(f"{where}: {name!r} needs exactly {sorted(_ENTRY_KEYS)}")
        description, schema_text, recorded, output_text = (
            entry["description"],
            entry["input_schema"],
            entry["sha256"],
            entry["output_schema"],
        )
        if not all(
            isinstance(value, str) for value in (description, schema_text, recorded, output_text)
        ):
            raise ToolPinsError(f"{where}: {name!r} holds a value that is not text")
        try:
            schema = json.loads(schema_text)
        except ValueError as error:
            raise ToolPinsError(f"{where}: the input schema of {name!r} is not JSON") from error
        if not isinstance(schema, dict):
            raise ToolPinsError(f"{where}: the input schema of {name!r} is not an object")
        try:
            output_schema = json.loads(output_text)
        except ValueError as error:
            raise ToolPinsError(f"{where}: the output schema of {name!r} is not JSON") from error
        if output_schema is not None and not isinstance(output_schema, dict):
            raise ToolPinsError(f"{where}: the output schema of {name!r} is not an object or null")
        if definition_sha256(name, description, schema, output_schema) != recorded:
            raise ToolPinsError(
                f"{where}: the hash of {name!r} is not the hash of its own text: the description or"
                " schema was edited without being pinned again (scripts/generate_tool_pins.py)"
            )
        pins[name] = ToolPin(
            description=description,
            input_schema=schema,
            sha256=recorded,
            output_schema=output_schema,
        )
    return ToolPins(pins)


def render_tool_pins(
    tools: Iterable[tuple[str, str | None, Mapping[str, Any], Mapping[str, Any] | None]],
) -> str:
    """The text of a pins file for these (name, description, input, output schema) tuples."""
    lines = [
        "# The reviewed description and schemas (input, output) of every tool the gateway offers.",
        "# A tool that is not here, or whose definition no longer matches, is hidden and refused",
        "# by the pinned_descriptions layer. Written by scripts/generate_tool_pins.py: review it.",
        "# Harborline Supply Co. is fictional.",
        "",
    ]
    for name, description, schema, output in sorted(tools, key=lambda item: item[0]):
        text = description or ""
        schema_text = json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False)
        output_text = json.dumps(output, indent=2, sort_keys=True, ensure_ascii=False)
        if "'''" in schema_text or "'''" in output_text:
            raise ToolPinsError(f"the schema of {name!r} holds ''', which a TOML literal cannot")
        lines += [
            f"[tools.{json.dumps(name)}]",
            f'sha256 = "{definition_sha256(name, text, schema, output)}"',
            f"description = {json.dumps(text, ensure_ascii=False)}",
            f"input_schema = '''\n{schema_text}\n'''",
            f"output_schema = '''\n{output_text}\n'''",
            "",
        ]
    return "\n".join(lines)
