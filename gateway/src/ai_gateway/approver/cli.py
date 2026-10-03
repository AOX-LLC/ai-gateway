"""gateway-approver: a person reviews and decides the writes that wait for approval.

Runs as the policy_approver database role (POLICY_APPROVER_DATABASE_URL). `--as` names the
approver, who must be registered and active (`gateway-admin approver-add`) and hold the request's
role. The database role is shared, so `--as` names who decided; it does not prove it. The dashboard
and the lab approver of later phases authenticate people themselves.
"""

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from uuid import UUID

import anyio
from aox_agent_core.approvals import (
    ApprovalRequest,
    Decision,
    Principal,
    PrincipalKind,
    RoleApproverPolicy,
)
from aox_agent_core.errors import AgentCoreError
from aox_agent_core.storage import open_database
from pydantic import SecretStr

from ai_gateway.approver.display import ApprovalNotShowableError, render
from ai_gateway.policy import approval_queue_on, policy_url
from ai_gateway.policy.roles import ApprovalRolesError, load_roles_by_action
from ai_gateway.text import printable

_URL_ENV = "POLICY_APPROVER_DATABASE_URL"
_ROLES_ENV = "APPROVAL_ROLES_FILE"
_DEFAULT_ROLES_FILE = "config/approval_roles.toml"


class ApproverError(Exception):
    pass


class Approvals:
    """The approver's view of the queue, and the one place that decides."""

    def __init__(self, database_url: str, roles_by_action: Mapping[str, str]) -> None:
        """`roles_by_action` is the role each write needs, from the file the requester cannot
        change: a request for any other action, or for another role, is refused."""
        self.database = open_database(SecretStr(policy_url(database_url)))
        self.queue = approval_queue_on(
            self.database, policy=RoleApproverPolicy(roles_by_action=roles_by_action)
        )

    async def principal(self, approver_id: str) -> Principal:
        def read(session: Any) -> list[tuple[Any, ...]]:
            rows: list[tuple[Any, ...]] = session.execute(
                "SELECT roles, active FROM policy.approvers WHERE id = ?", (approver_id,)
            )
            return rows

        rows = await self.database.run(read)
        if not rows or not rows[0][1]:
            raise ApproverError(
                f"{printable(approver_id, 80)} is not a registered, active approver"
            )
        return Principal(
            id=f"human:{approver_id}", kind=PrincipalKind.HUMAN, roles=frozenset(rows[0][0])
        )

    async def arguments_json(self, request_id: UUID) -> str | None:
        def read(session: Any) -> list[tuple[Any, ...]]:
            rows: list[tuple[Any, ...]] = session.execute(
                "SELECT arguments_json FROM policy.approval_arguments WHERE request_id = ?",
                (str(request_id),),
            )
            return rows

        rows = await self.database.run(read)
        return str(rows[0][0]) if rows else None

    async def show(self, request_id: UUID) -> tuple[ApprovalRequest, str]:
        request = await self.queue.get(request_id)
        return request, render(request, await self.arguments_json(request_id)).text

    async def decide(
        self, request_id: UUID, approver_id: str, decision: Decision, reason: str | None
    ) -> ApprovalRequest:
        """Check the request can be shown as asked for, then decide it. Rejecting needs no
        arguments: a person can always refuse."""
        principal = await self.principal(approver_id)
        if decision is Decision.APPROVE:
            await self.show(request_id)  # raises if it cannot be shown
        return await self.queue.resolve(
            request_id, decision=decision, principal=principal, reason=reason
        )


async def _list(approvals: Approvals, args: argparse.Namespace) -> None:
    principal = await approvals.principal(args.approver)
    pending = await approvals.queue.list_pending(principal)
    if not pending:
        print("nothing waiting")
    for request in pending:
        print(
            f"{request.id}  {printable(request.action, 80)}  asked by "
            f"{printable(request.requested_by, 80)}  expires {request.expires_at.isoformat()}"
        )


async def _show(approvals: Approvals, args: argparse.Namespace) -> None:
    await approvals.principal(args.approver)
    print((await approvals.show(args.request))[1])


async def _decide(approvals: Approvals, args: argparse.Namespace) -> None:
    decision = Decision.APPROVE if args.command == "approve" else Decision.REJECT
    await approvals.principal(args.approver)
    if decision is Decision.APPROVE:
        _, text = await approvals.show(args.request)
        print(text)
        if not args.yes:
            answer = await anyio.to_thread.run_sync(
                input, "approve exactly this call? type 'yes': "
            )
            if answer.strip() != "yes":
                raise ApproverError("not approved")
    request = await approvals.decide(args.request, args.approver, decision, args.reason)
    print(f"{request.status.value}: {request.id}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gateway-approver", description=__doc__)
    parser.add_argument("--as", dest="approver", required=True, help="your approver id")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="requests waiting for you").set_defaults(handler=_list)
    for name, handler in (("show", _show), ("approve", _decide), ("reject", _decide)):
        command = commands.add_parser(name, help=f"{name} a request")
        command.add_argument("request", type=UUID)
        command.set_defaults(handler=handler)
        if name != "show":
            command.add_argument("--reason", default=None)
        if name == "approve":
            command.add_argument("--yes", action="store_true", help="do not ask to confirm")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    url = os.environ.get(_URL_ENV)
    if not url:
        sys.exit(f"gateway-approver: {_URL_ENV} is not set")
    try:
        roles = load_roles_by_action(Path(os.environ.get(_ROLES_ENV, _DEFAULT_ROLES_FILE)))
        anyio.run(args.handler, Approvals(url, roles), args)
    except (ApproverError, ApprovalNotShowableError, ApprovalRolesError, AgentCoreError) as error:
        message = f"{type(error).__name__}: {error}" if isinstance(error, AgentCoreError) else error
        sys.exit(f"gateway-approver: {printable(str(message), 300)}")


if __name__ == "__main__":
    main()
