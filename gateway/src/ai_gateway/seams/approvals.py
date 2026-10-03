"""The seam where human approval of write tools plugs in.

The approval layer asks an ApprovalGate whether a write may run. The gate submits the call to the
approval queue, holds for a person's decision for a short while, and checks right before the call
is forwarded that the approval was granted for exactly this tool and these arguments and was asked
for by this client. The pipeline already guarantees the payload cannot change after the approval
layer runs: ToolCall holds its arguments as immutable canonical JSON, and approval is the last
layer before the upstream call.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from ai_gateway.pipeline.types import CallContext, ToolCall


class ApprovalOutcome(StrEnum):
    APPROVED = "approved"
    """A person approved this call and the approval was used up for it: forward it."""
    PENDING = "pending"
    """No decision yet: the client is told to retry the same call."""
    REJECTED = "rejected"
    EXPIRED = "expired"
    UNAVAILABLE = "unavailable"
    """The queue cannot be used. The call is refused."""


@dataclass(frozen=True)
class ApprovalDecision:
    outcome: ApprovalOutcome
    approval_id: str | None = None


class ApprovalGate(Protocol):
    async def decide(self, ctx: CallContext, call: ToolCall) -> ApprovalDecision:
        """Find or ask for this client's approval of this call and hold for a decision.

        Never raises for a failing queue: that is UNAVAILABLE."""
        ...
