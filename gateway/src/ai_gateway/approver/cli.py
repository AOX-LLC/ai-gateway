"""gateway-approver: a person reviews and decides the writes that wait for approval.

A person signs in to the database with a login of their own (`gateway-admin approver-add` makes
it), and who they are is read from that session: the approver whose login it is, who must be active
and hold the request's role. There is nothing to claim, and the audit log records the login that
wrote each decision, so a decision cannot name anyone else. POLICY_APPROVER_DATABASE_URL names the
database; the login and password come from APPROVER_LOGIN and APPROVER_PASSWORD or a prompt. A URL
that carries a login (the test tooling's) is used as it is.
"""

import argparse
import getpass
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit
from uuid import UUID

import anyio
import psycopg
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
_LOGIN_ENV = "APPROVER_LOGIN"
_PASSWORD_ENV = "APPROVER_PASSWORD"  # noqa: S105 - the name of a variable, not a password
_ROLES_ENV = "APPROVAL_ROLES_FILE"
_DEFAULT_ROLES_FILE = "config/approval_roles.toml"


class ApproverError(Exception):
    pass


class Approvals:
    """The approver's view of the queue, and the one place that decides."""

    def __init__(self, database_url: str, roles_by_action: Mapping[str, str]) -> None:
        """`roles_by_action` is the role each write needs, from the file the requester cannot
        change: a request for any other action, or for another role, is refused."""
        # One connection: the login allows two, and the tool does one thing at a time.
        self.database = open_database(SecretStr(policy_url(database_url)), max_connections=1)
        self.queue = approval_queue_on(
            self.database, policy=RoleApproverPolicy(roles_by_action=roles_by_action)
        )

    async def principal(self) -> Principal:
        """Who is signed in: the active approver whose database login this session is."""

        async def read(session: Any) -> list[tuple[Any, ...]]:
            rows: list[tuple[Any, ...]] = await session.execute(
                "SELECT id, roles, active FROM policy.approvers WHERE db_role = current_user"
            )
            return rows

        try:
            rows = await self.database.run(read)
        except psycopg.errors.InsufficientPrivilege:
            rows = []  # a login that may not even read the approvers is no approver's
        if not rows or not rows[0][2]:
            raise ApproverError("this database login is not a registered, active approver")
        return Principal(
            id=f"human:{rows[0][0]}", kind=PrincipalKind.HUMAN, roles=frozenset(rows[0][1])
        )

    async def arguments_json(self, request_id: UUID) -> str | None:
        async def read(session: Any) -> list[tuple[Any, ...]]:
            rows: list[tuple[Any, ...]] = await session.execute(
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
        self, request_id: UUID, decision: Decision, reason: str | None
    ) -> ApprovalRequest:
        """Check the request can be shown as asked for, then decide it as whoever is signed in.
        Rejecting needs no arguments: a person can always refuse."""
        principal = await self.principal()
        if decision is Decision.APPROVE:
            await self.show(request_id)  # raises if it cannot be shown
        return await self.queue.resolve(
            request_id, decision=decision, principal=principal, reason=reason
        )


async def _run(approvals: Approvals, args: argparse.Namespace) -> None:
    """Run the command, then close the database the queue opened."""
    try:
        await args.handler(approvals, args)
    finally:
        await approvals.database.aclose()


async def _whoami(approvals: Approvals, args: argparse.Namespace) -> None:
    principal = await approvals.principal()
    print(f"{principal.id}  roles: {', '.join(sorted(principal.roles))}")


async def _list(approvals: Approvals, args: argparse.Namespace) -> None:
    principal = await approvals.principal()
    pending = await approvals.queue.list_pending(principal)
    if not pending:
        print("nothing waiting")
    for request in pending:
        print(
            f"{request.id}  {printable(request.action, 80)}  asked by "
            f"{printable(request.requested_by, 80)}  expires {request.expires_at.isoformat()}"
        )


async def _show(approvals: Approvals, args: argparse.Namespace) -> None:
    await approvals.principal()
    print((await approvals.show(args.request))[1])


async def _decide(approvals: Approvals, args: argparse.Namespace) -> None:
    decision = Decision.APPROVE if args.command == "approve" else Decision.REJECT
    principal = await approvals.principal()
    if decision is Decision.APPROVE:
        _, text = await approvals.show(args.request)
        print(text)
        if not args.yes:
            answer = await anyio.to_thread.run_sync(
                input, "approve exactly this call? type 'yes': "
            )
            if answer.strip() != "yes":
                raise ApproverError("not approved")
    request = await approvals.decide(args.request, decision, args.reason)
    print(f"{request.status.value}: {request.id} (decided by {principal.id})")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gateway-approver", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("whoami", help="who this login is").set_defaults(handler=_whoami)
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


def _sign_in(url: str) -> str:
    """The URL with the person's login in it. A URL that already carries one is left as it is."""
    parts = urlsplit(url)
    if parts.username:
        return url
    try:
        login = os.environ.get(_LOGIN_ENV) or input("approver login: ")
        password = os.environ.get(_PASSWORD_ENV) or getpass.getpass("password: ")
    except EOFError:
        raise ApproverError(
            f"no terminal to ask on: set {_LOGIN_ENV} and {_PASSWORD_ENV} for this one command"
        ) from None
    host = parts.netloc.rpartition("@")[2]  # as written: an IPv6 address keeps its brackets
    netloc = f"{quote(login.strip(), safe='')}:{quote(password, safe='')}@{host}"
    return urlunsplit(parts._replace(netloc=netloc))


def main(argv: Sequence[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    url = os.environ.get(_URL_ENV)
    if not url:
        sys.exit(f"gateway-approver: {_URL_ENV} is not set")
    try:
        roles = load_roles_by_action(Path(os.environ.get(_ROLES_ENV, _DEFAULT_ROLES_FILE)))
        anyio.run(_run, Approvals(_sign_in(url), roles), args)
    except psycopg.OperationalError:
        # The server's message names the login and may say why; one line is enough, and no detail
        # of a failed sign-in is worth showing to whoever is typing.
        sys.exit(
            "gateway-approver: could not sign in to the database: check the login and password"
        )
    except (ApproverError, ApprovalNotShowableError, ApprovalRolesError, AgentCoreError) as error:
        message = f"{type(error).__name__}: {error}" if isinstance(error, AgentCoreError) else error
        sys.exit(f"gateway-approver: {printable(str(message), 300)}")


if __name__ == "__main__":
    main()
