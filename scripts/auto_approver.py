"""TEST TOOLING ONLY: plays the human who approves, so a scripted client's writes can run.

A write waits for a person's approval, and the scenario and the simulator have no person. This
approves whatever is pending, as a registered approver, while they run. It is not part of the
gateway and never runs there; it needs both of

    --approve-as <approver id>        and        LAB_AUTO_APPROVE=yes in the environment

and it approves through the same code the approver's CLI uses, so a request whose stored arguments
do not hash to what was asked for is still refused. Run against anything but a fictional demo
stack, it would defeat the point of approval.
"""

import os
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import anyio
from aox_agent_core.approvals import Decision

from ai_gateway.approver.cli import Approvals

POLL_S = 0.2


@dataclass
class AutoApproval:
    approved: int = 0
    refused: int = 0


@asynccontextmanager
async def auto_approving(approver_id: str | None) -> AsyncGenerator[AutoApproval]:
    """Approve what is pending while the body runs. With no approver id, do nothing."""
    result = AutoApproval()
    if approver_id is None:
        yield result
        return
    if os.environ.get("LAB_AUTO_APPROVE") != "yes":
        sys.exit("auto approval needs LAB_AUTO_APPROVE=yes (it is for the fictional demo stack)")
    url = os.environ.get("POLICY_APPROVER_DATABASE_URL")
    if not url:
        sys.exit("auto approval needs POLICY_APPROVER_DATABASE_URL")
    print(f"AUTO-APPROVING pending writes as {approver_id} (test tooling, LAB_AUTO_APPROVE=yes)")
    approvals = Approvals(url)
    principal = await approvals.principal(approver_id)

    async def loop() -> None:
        while True:
            for request in await approvals.queue.list_pending(principal):
                try:
                    await approvals.decide(request.id, approver_id, Decision.APPROVE, None)
                    result.approved += 1
                except Exception:
                    result.refused += 1
            await anyio.sleep(POLL_S)

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(loop)
        try:
            yield result
        finally:
            tasks.cancel_scope.cancel()
