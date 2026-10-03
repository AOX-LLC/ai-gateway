"""The lab approver: approves every pending write, as a registered approver, until stopped.

For the lab Compose profile (`docker compose --profile lab up`), which Phase 6's red-team runs use
so that a scripted attacker's writes get through the approval layer and what is measured is the
other layers. It is not part of the gateway and not in any image: Compose mounts it into the one
`lab-approver` service. It needs LAB_AUTO_APPROVE=yes (the service will not start without it) and
its own database role, which exists only while the stack is set up with that role's password.
Run against anything real, it would defeat approval entirely.
"""

import os

import anyio

from auto_approver import auto_approving


async def main() -> None:
    approver_id = os.environ.get("LAB_APPROVER_ID", "lab-approver")
    async with auto_approving(approver_id):
        await anyio.sleep_forever()


if __name__ == "__main__":
    anyio.run(main)
