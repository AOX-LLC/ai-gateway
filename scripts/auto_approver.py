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
from pathlib import Path

import anyio
from aox_agent_core.approvals import Decision

from ai_gateway.approver.cli import Approvals
from ai_gateway.policy.roles import load_roles_by_action

POLL_S = 0.2


@dataclass
class AutoApproval:
    approved: int = 0
    refused: int = 0


@asynccontextmanager
async def auto_approving(
    approver_id: str | None, decision: Decision = Decision.APPROVE
) -> AsyncGenerator[AutoApproval]:
    """Decide what is pending while the body runs: approve it (the default), or reject it (the
    scorecard's idealised approver, who never misses an attack). With no approver id, do nothing."""
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
    roles_file = Path(os.environ.get("APPROVAL_ROLES_FILE", "config/approval_roles.toml"))
    approvals = Approvals(url, load_roles_by_action(roles_file))
    principal = await approvals.principal()
    if principal.id != f"human:{approver_id}":
        sys.exit(f"auto approval: the login is {principal.id}, not {approver_id}")
    print(
        f"READY: lab approver {approver_id} will {decision.value} every pending write", flush=True
    )

    async def loop() -> None:
        while True:
            for request in await approvals.queue.list_pending(principal):
                try:
                    await approvals.decide(request.id, decision, None)
                    result.approved += 1
                except Exception:
                    result.refused += 1
            await anyio.sleep(POLL_S)

    try:
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(loop)
            try:
                yield result
            finally:
                tasks.cancel_scope.cancel()
    finally:
        # The queue's connection pool is closed, not left to reconnect in the background: a pool
        # left open once held the process (and the scenario script) up after the work was done.
        with anyio.CancelScope(shield=True):
            await approvals.database.aclose()
