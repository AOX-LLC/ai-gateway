"""The reviewed tool policy file: which upstream tools only read and which write."""

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ai_gateway.pipeline.types import Effect

_NAMESPACE = re.compile(r"^[a-z][a-z0-9]*(_[a-z0-9]+)*$")
_TOOL = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_EFFECTS: tuple[Effect, ...] = ("read", "write")
_ALLOWED_KEYS = {"effect", "notes"}


class ToolPolicyFileError(ValueError):
    """The policy file is malformed. Reported before anything is written to the registry."""


@dataclass(frozen=True)
class ToolPolicy:
    namespace: str
    tool: str
    effect: Effect
    notes: str = ""


def load_tool_policies(path: Path) -> list[ToolPolicy]:
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ToolPolicyFileError(f"cannot read {path}: {error}") from error

    policies = []
    for namespace, tools in document.items():
        if _NAMESPACE.fullmatch(namespace) is None or not isinstance(tools, dict):
            raise ToolPolicyFileError(f"{path}: {namespace!r} is not a namespace table")
        for tool, entry in tools.items():
            policies.append(_policy(path, namespace, tool, entry))
    return policies


def _policy(path: Path, namespace: str, tool: str, entry: Any) -> ToolPolicy:
    where = f"{path}: [{namespace}.{tool}]"
    if _TOOL.fullmatch(tool) is None or not isinstance(entry, dict):
        raise ToolPolicyFileError(f"{where} is not a tool table")
    unknown = entry.keys() - _ALLOWED_KEYS
    if unknown:
        raise ToolPolicyFileError(f"{where} has unknown keys: {sorted(unknown)}")
    effect = entry.get("effect")
    if effect not in _EFFECTS:
        raise ToolPolicyFileError(f"{where} needs effect = 'read' or 'write'")
    notes = entry.get("notes", "")
    if not isinstance(notes, str):
        raise ToolPolicyFileError(f"{where} notes must be text")
    return ToolPolicy(namespace, tool, effect, notes)
