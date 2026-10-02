"""The seam where human approval of write tools plugs in.

A later phase implements ApprovalGate on top of the shared agent-core library's
approval queue, which is pre-release and unmerged; its interface may shift before
v0.1.0. The plan for that layer: submit the call, wait for a human decision, then check
right before forwarding that the approval was granted for exactly this payload. The
pipeline already guarantees the payload cannot change after the approval layer runs:
ToolCall holds its arguments as immutable canonical JSON, and approval is the last
layer before the upstream call.
"""

from enum import StrEnum
from typing import Protocol

from ai_gateway.pipeline.types import CallContext, ToolCall


class ApprovalOutcome(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class ApprovalGate(Protocol):
    async def await_decision(self, ctx: CallContext, call: ToolCall) -> ApprovalOutcome:
        """Block until a human approves or rejects this call, or the request expires."""
        ...
