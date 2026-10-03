"""What an approver is shown, and the check that it is what was asked for.

The arguments are shown as JSON with every non-ASCII and control character escaped (`\\u001b`), so
nothing in them can move the cursor, recolour the screen or hide text, and the person still sees
exactly what the call says. Free text from outside (a client's name, a summary) has control
characters and escape sequences removed. Before anything is shown as approvable, the arguments are
parsed back from the very text on screen and hashed with the tool name: the hash must be the one the
gateway stored when it asked."""

import json
from dataclasses import dataclass
from typing import Any

from aox_agent_core.approvals import ApprovalRequest, approval_payload_hash

from ai_gateway.text import printable

MAX_LINE = 200


class ApprovalNotShowableError(Exception):
    """The request cannot be shown as it was asked for: nobody should approve it."""


@dataclass(frozen=True)
class Rendered:
    text: str
    suspicious: list[str]
    """Values that held control characters or escape sequences: they are shown escaped."""


def _strings(value: Any, path: str = "") -> list[tuple[str, str]]:
    if isinstance(value, str):
        return [(path or "(root)", value)]
    if isinstance(value, dict):
        return [item for k, v in value.items() for item in _strings(v, f"{path}.{k}".lstrip("."))]
    if isinstance(value, list):
        return [item for i, v in enumerate(value) for item in _strings(v, f"{path}[{i}]")]
    return []


def render(request: ApprovalRequest, arguments_json: str | None) -> Rendered:
    """The request as an approver should see it, or ApprovalNotShowableError."""
    if arguments_json is None:
        raise ApprovalNotShowableError(
            "the arguments of this request are not stored (purged, or never saved): refusing"
        )
    try:
        arguments = json.loads(arguments_json)
    except ValueError:
        raise ApprovalNotShowableError("the stored arguments are not JSON: refusing") from None
    if not isinstance(arguments, dict):
        raise ApprovalNotShowableError("the stored arguments are not an object: refusing")

    shown = json.dumps(arguments, indent=2, sort_keys=True, ensure_ascii=True)
    # The text on screen, read back, must be what was asked for.
    if approval_payload_hash(request.action, json.loads(shown)) != request.payload_sha256:
        raise ApprovalNotShowableError(
            "the stored arguments do not match the request's hash: refusing"
        )
    suspicious = [
        path for path, text in _strings(arguments) if printable(text) != text or not text.isascii()
    ]
    lines = [
        f"request    {request.id}",
        f"tool       {printable(request.action, 100)}",
        f"asked by   {printable(request.requested_by, 150)}",
        f"summary    {printable(request.summary, 300)}",
        f"asked at   {request.created_at.isoformat()}",
        f"expires    {request.expires_at.isoformat()}",
        f"hash       {request.payload_sha256[:16]}... verified against the arguments below",
        "arguments",
        *(f"  {line}" for line in shown.splitlines()),
    ]
    if suspicious:
        lines.append(
            "note       non-ASCII or control characters are shown escaped, in: "
            + ", ".join(printable(path, 80) for path in suspicious)
        )
    return Rendered("\n".join(lines), suspicious)
