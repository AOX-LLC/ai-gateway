"""Which role must approve which write, read from a file the requester cannot choose."""

import tomllib
from collections.abc import Mapping
from pathlib import Path


class ApprovalRolesError(ValueError):
    pass


def load_roles_by_action(path: Path) -> Mapping[str, str]:
    """The `[roles_by_action]` table: tool name to role. A mistake stops startup."""
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ApprovalRolesError(f"cannot read the approval roles {path}: {error}") from error
    unknown = set(raw) - {"roles_by_action"}
    if unknown:
        raise ApprovalRolesError(f"unknown keys in {path}: {sorted(unknown)}")
    table = raw.get("roles_by_action", {})
    if not isinstance(table, dict) or not all(
        isinstance(action, str) and isinstance(role, str) and action and role
        for action, role in table.items()
    ):
        raise ApprovalRolesError(f"[roles_by_action] in {path} must map tool names to roles")
    return dict(table)
