"""The approval layer: a write runs only after a person has approved exactly that call."""

from ai_gateway.pipeline.types import (
    ALLOW,
    Allow,
    BaseLayer,
    CallContext,
    Deny,
    DenyCode,
    Disposition,
    ToolCall,
    Verdict,
)
from ai_gateway.seams.approvals import ApprovalGate, ApprovalOutcome

PENDING_MESSAGE = (
    "This call needs a person's approval. It has been submitted; retry the same call"
    " (same tool, same arguments) shortly."
)
_DENIALS = {
    ApprovalOutcome.REJECTED: (DenyCode.APPROVAL_REJECTED, "An approver rejected this call."),
    ApprovalOutcome.EXPIRED: (DenyCode.APPROVAL_EXPIRED, "The approval for this call expired."),
    ApprovalOutcome.UNAVAILABLE: (
        DenyCode.APPROVAL_UNAVAILABLE,
        "Request blocked by gateway policy.",
    ),
}


class ApprovalLayer(BaseLayer):
    """Reads pass. A write needs an approval, and with no gate (no policy database) it is refused:
    the layer fails closed."""

    name = "approval"
    floor = True

    def __init__(self, gate: ApprovalGate | None = None) -> None:
        self._gate = gate
        self.observe_only = False
        """Set by the pipeline in monitor mode: a write that would need approval is reported and
        goes on, and nobody is asked, because asking is a side effect."""

    async def before_call(self, ctx: CallContext, call: ToolCall) -> Verdict:
        if call.effect != "write":
            return ALLOW
        if self.observe_only:
            return Deny(DenyCode.APPROVAL_PENDING, PENDING_MESSAGE, Disposition.PENDING)
        if self._gate is None:
            code, message = _DENIALS[ApprovalOutcome.UNAVAILABLE]
            return Deny(code, message)
        decision = await self._gate.decide(ctx, call)
        if decision.outcome is ApprovalOutcome.APPROVED:
            return Allow(approval_id=decision.approval_id)
        if decision.outcome is ApprovalOutcome.PENDING:
            return Deny(
                DenyCode.APPROVAL_PENDING,
                PENDING_MESSAGE,
                Disposition.PENDING,
                decision.approval_id,
            )
        code, message = _DENIALS[decision.outcome]
        return Deny(code, message, approval_id=decision.approval_id)
