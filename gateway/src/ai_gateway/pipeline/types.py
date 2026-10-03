"""The request pipeline's vocabulary: layers, verdicts, and what they inspect."""

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from functools import cached_property
from typing import Any, ClassVar, Literal
from uuid import UUID

from mcp.types import CallToolResult, Tool

from ai_gateway.text import printable

Effect = Literal["read", "write"]
"""Whether a tool only reads or may change something. Decided by the gateway's reviewed
policy, never by the upstream's own annotations."""

EffectSource = Literal["policy", "default"]
"""Where an effect came from. "default" means no policy covers the tool, so it is a write."""


class LayerMode(StrEnum):
    ENFORCE = "enforce"
    """A Deny stops the request."""
    MONITOR = "monitor"
    """A Deny is recorded as would-block and the request goes on."""
    OFF = "off"
    """The layer does not run."""


class DenyCode(StrEnum):
    TOOL_UNAVAILABLE = "tool_unavailable"
    LAYER_ERROR = "layer_error"
    AUDIT_UNAVAILABLE = "audit_unavailable"
    APPROVAL_PENDING = "approval_pending"
    APPROVAL_REJECTED = "approval_rejected"
    APPROVAL_EXPIRED = "approval_expired"
    APPROVAL_UNAVAILABLE = "approval_unavailable"


class Disposition(StrEnum):
    """What the client is told when a layer says no."""

    BLOCK = "block"
    """The call is refused: an error."""
    PENDING = "pending"
    """The call is not refused for good: a person has been asked, and the client retries the same
    call later. Nothing was forwarded, and it is recorded as a block."""


@dataclass(frozen=True)
class Allow:
    approval_id: str | None = None
    """The approval this call used, for the record."""


@dataclass(frozen=True)
class Deny:
    code: DenyCode
    public_message: str
    """Safe to show the client. Never names the layer or echoes tool arguments."""
    disposition: Disposition = Disposition.BLOCK
    approval_id: str | None = None
    """The approval request behind this verdict, when there is one: it is the client's receipt."""


Verdict = Allow | Deny
ALLOW = Allow()


_MAX_ECHOED_NAME_LENGTH = 64


def displayable_tool_name(requested_name: str) -> str:
    """A client-supplied tool name, cut to a length and cleaned of control characters and escape
    sequences, so it is safe to echo, record and print.

    The SDK accepts names of any length; no real tool name is longer than 64 characters.
    """
    return printable(requested_name, _MAX_ECHOED_NAME_LENGTH)


def tool_unavailable_message(exposed_name: str) -> str:
    """One message for both unknown and out-of-scope tools, so a client cannot use
    the difference to discover tools it was not granted."""
    return f"Tool '{displayable_tool_name(exposed_name)}' is not available to this client."


@dataclass(frozen=True)
class ClientIdentity:
    id: UUID
    name: str
    scopes: frozenset[str]

    @property
    def actor_id(self) -> str:
        return f"client:{self.id}"


@dataclass(frozen=True)
class CallContext:
    request_id: UUID
    client: ClientIdentity
    session_id: str | None
    protocol_version: str


@dataclass(frozen=True)
class CatalogTool:
    """An upstream tool as clients see it: renamed to its exposed, namespaced name."""

    namespace: str
    upstream_name: str
    tool: Tool
    effect: Effect = "write"
    """Fail closed: a tool nobody has classified is treated as a write."""
    effect_source: EffectSource = "default"

    @property
    def exposed_name(self) -> str:
        return self.tool.name


@dataclass(frozen=True)
class ToolCall:
    """A tools/call request. The arguments are held as canonical JSON, so no layer can
    change them in place: what the last layer saw is exactly what is forwarded."""

    exposed_name: str
    namespace: str
    upstream_tool: str
    arguments_json: str = field(repr=False)
    effect: Effect = "write"
    effect_source: EffectSource = "default"
    upstream_identity: str = ""
    """Which upstream this call goes to (`UpstreamServer.identity`). An approval is bound to it."""

    @classmethod
    def create(
        cls,
        exposed_name: str,
        namespace: str,
        upstream_tool: str,
        arguments: dict[str, Any],
        effect: Effect = "write",
        effect_source: EffectSource = "default",
        upstream_identity: str = "",
    ) -> "ToolCall":
        canonical = json.dumps(arguments, sort_keys=True, separators=(",", ":"))
        return cls(
            exposed_name,
            namespace,
            upstream_tool,
            canonical,
            effect,
            effect_source,
            upstream_identity,
        )

    @property
    def arguments(self) -> dict[str, Any]:
        """A fresh copy on every access."""
        parsed: dict[str, Any] = json.loads(self.arguments_json)
        return parsed

    @cached_property
    def arguments_sha256(self) -> str:
        return hashlib.sha256(self.arguments_json.encode()).hexdigest()


class BaseLayer:
    """One check in the pipeline. Override only the hooks the layer needs.

    Layers return verdicts; they never call upstream. The runner owns the upstream
    call, so a layer cannot skip the layers after it or call a tool twice.
    """

    name: ClassVar[str]
    floor: ClassVar[bool] = False
    """A floor layer may only be weakened with the config's allow_floor_override flag."""

    async def filter_tools(
        self, ctx: CallContext, tools: Sequence[CatalogTool]
    ) -> Sequence[CatalogTool]:
        return tools

    async def before_call(self, ctx: CallContext, call: ToolCall) -> Verdict:
        return ALLOW

    async def after_call(self, ctx: CallContext, call: ToolCall, result: CallToolResult) -> Verdict:
        return ALLOW
