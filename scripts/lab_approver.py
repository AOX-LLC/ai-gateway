"""The lab approver: approves every pending write, as a registered approver, until stopped.

For the lab Compose profile (`docker compose --profile lab up`), which Phase 6's red-team runs use
so that a scripted attacker's writes get through the approval layer and what is measured is the
other layers. It is not part of the gateway and not in any image: Compose mounts it into the one
`lab-approver` service. It needs LAB_AUTO_APPROVE=yes (the service will not start without it) and
its own database role, which exists only while the stack is set up with that role's password.
Run against anything real, it would defeat approval entirely.
"""

import os
import sys

import anyio
from aox_agent_core.approvals import Decision

from auto_approver import auto_approving


def decision_from_env(value: str | None) -> Decision:
    """`approve` (the default) or `reject`: what this approver does with every pending write. The
    scorecard runs its idealised column with an approver that rejects. Anything else stops it."""
    if value in (None, "", "approve"):
        return Decision.APPROVE
    if value == "reject":
        return Decision.REJECT
    sys.exit(f"lab_approver: LAB_APPROVER_DECISION is approve or reject, not {value!r}")


async def main() -> None:
    approver_id = os.environ.get("LAB_APPROVER_ID", "lab-approver")
    decision = decision_from_env(os.environ.get("LAB_APPROVER_DECISION"))
    async with auto_approving(approver_id, decision):
        await anyio.sleep_forever()


if __name__ == "__main__":
    anyio.run(main)
