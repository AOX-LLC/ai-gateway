"""gateway-admin: manage clients, tokens and upstreams in the registry.

Runs as the database owner (GATEWAY_MIGRATE_DATABASE_URL), never inside the gateway
container. A new token is printed once, to stdout, and is not stored anywhere.
"""

import argparse
import json
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import anyio
from aox_agent_core.audit import SQLAuditLog
from aox_agent_core.errors import AuditIntegrityError, ConfigError
from aox_agent_core.storage import open_database
from psycopg import AsyncConnection
from psycopg.errors import CheckViolation
from pydantic import SecretStr

from ai_gateway.auth.tokens import IssuedToken, generate_token
from ai_gateway.policy import audit_log_on, policy_url
from ai_gateway.policy.anchors import (
    AnchorFileError,
    append_anchor,
    read_anchors,
    verify_with_anchors,
)
from ai_gateway.policy.setup import PolicyPasswords, setup_policy
from ai_gateway.proxy.naming import split_exposed
from ai_gateway.registry import MIGRATIONS_PACKAGE
from ai_gateway.registry.models import CREDENTIAL_ENV_SUFFIX, ClientStatus
from ai_gateway.registry.repo import AdminRegistry, ClientNotFoundError
from ai_gateway.registry.tool_policies import ToolPolicyFileError, load_tool_policies
from ai_gateway.telemetry.setup import TelemetryPasswords, setup_telemetry
from mcp_common.migrate import apply_migrations

_DATABASE_URL_ENV = "GATEWAY_MIGRATE_DATABASE_URL"
_AUDITOR_URL_ENV = "POLICY_AUDITOR_DATABASE_URL"
MAX_LIVE_TOKENS_PER_CLIENT = 2

# Fictional demo data for the test profile. Harborline Supply Co. does not exist.
TEST_ECHO_NAMESPACE = "echo"
TEST_CLIENTS = {
    "echo-test-narrow": ("Test client that may only call echo__say (test data)", ["echo__say"]),
    "echo-test-wide": (
        "Test client that may call both echo tools (test data)",
        ["echo__say", "echo__shout"],
    ),
}

# Fictional demo data: the three Harborline servers' tools and the two clients that use them.
DEMO_TICKETS_NAMESPACE = "tickets"
DEMO_CRM_NAMESPACE = "crm"
DEMO_HANDBOOK_NAMESPACE = "handbook"
DEMO_SCOPES_SUPPORT = [
    "handbook__search",
    "handbook__get_document",
    "crm__search_accounts",
    "crm__get_account",
    "crm__list_deals",
    "tickets__get_ticket",
    "tickets__list_tickets",
    "tickets__create_ticket",
    "tickets__add_comment",
]
DEMO_SCOPES_OPS = [
    *DEMO_SCOPES_SUPPORT,
    "tickets__change_status",
    "tickets__assign",
]
DEMO_CLIENTS = {
    "harborline-support-bot": (
        "Support assistant for the fictional Harborline Supply Co. (demo data)",
        DEMO_SCOPES_SUPPORT,
    ),
    "harborline-ops-bot": (
        "Operations assistant for the fictional Harborline Supply Co. (demo data)",
        DEMO_SCOPES_OPS,
    ),
}


class AdminError(Exception):
    """An operator mistake, reported as one line without a traceback."""


def main(argv: Sequence[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    # Most commands run as the database owner; the audit commands run as a role that can only read.
    database_env = getattr(args, "database_env", _DATABASE_URL_ENV)
    database_url = os.environ.get(database_env)
    if not database_url:
        sys.exit(f"gateway-admin: {database_env} is not set")
    try:
        anyio.run(args.handler, database_url, args)
    except (
        AdminError,
        ClientNotFoundError,
        AnchorFileError,
        AuditIntegrityError,
        ConfigError,
        OSError,
    ) as error:
        sys.exit(f"gateway-admin: {error}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gateway-admin", description=__doc__)
    commands = parser.add_subparsers(required=True, metavar="command")

    migrate = commands.add_parser("migrate", help="apply database migrations")
    migrate.set_defaults(handler=_migrate)

    telemetry_setup = commands.add_parser(
        "telemetry-setup",
        help="create the telemetry roles and schema, migrate and grant (safe to repeat)",
    )
    telemetry_setup.set_defaults(handler=_telemetry_setup)

    policy_setup = commands.add_parser(
        "policy-setup",
        help="create the policy roles and schema, install the audit log and approval queue,"
        " and grant (safe to repeat)",
    )
    policy_setup.set_defaults(handler=_policy_setup)

    anchor = commands.add_parser(
        "audit-anchor", help="append the audit log's head to an anchor file kept elsewhere"
    )
    anchor.add_argument("--file", type=Path, required=True)
    anchor.set_defaults(handler=_audit_anchor, database_env=_AUDITOR_URL_ENV)

    verify = commands.add_parser(
        "audit-verify", help="check the audit log's hash chain, and every anchor against it"
    )
    verify.add_argument("--anchors", type=Path, help="an anchor file; without one, only the chain")
    verify.set_defaults(handler=_audit_verify, database_env=_AUDITOR_URL_ENV)

    approver_add = commands.add_parser(
        "approver-add", help="register (or update) a person who may approve writes"
    )
    approver_add.add_argument("id", help="lowercase letters, digits, . _ -")
    approver_add.add_argument("--name", required=True, help="who they are, for the log")
    approver_add.add_argument("--role", action="append", default=None, help="default: approver")
    approver_add.set_defaults(handler=_approver_add)
    commands.add_parser("approver-list", help="list approvers").set_defaults(handler=_approver_list)
    approver_off = commands.add_parser("approver-deactivate", help="stop an approver deciding")
    approver_off.add_argument("id")
    approver_off.set_defaults(handler=_approver_deactivate)

    client_add = commands.add_parser("client-add", help="create or update a client")
    client_add.add_argument("name")
    client_add.add_argument("--description", default="")
    client_add.set_defaults(handler=_client_add)

    for status in ClientStatus:
        set_status = commands.add_parser(
            f"client-{_verb(status)}", help=f"{_verb(status)} a client"
        )
        set_status.add_argument("name")
        set_status.set_defaults(handler=_client_set_status, status=status)

    for verb in ("grant", "revoke"):
        scope = commands.add_parser(f"scope-{verb}", help=f"{verb} tool scopes, e.g. echo__say")
        scope.add_argument("name")
        scope.add_argument("tools", nargs="+")
        scope.set_defaults(handler=_scope_change, verb=verb)

    issue = commands.add_parser("token-issue", help="issue a token (printed once)")
    issue.add_argument("--client", required=True)
    issue.add_argument("--expires-in-days", type=_positive_int)
    issue.add_argument("--label", default="")
    issue.set_defaults(handler=_token_issue)

    rotate = commands.add_parser(
        "token-rotate", help="issue a new token; the old ones expire after a grace period"
    )
    rotate.add_argument("--client", required=True)
    rotate.add_argument("--grace-hours", type=float, default=24.0)
    rotate.set_defaults(handler=_token_rotate)

    revoke = commands.add_parser("token-revoke", help="revoke a token now, by its lookup id")
    revoke.add_argument("lookup_id")
    revoke.set_defaults(handler=_token_revoke)

    upstream = commands.add_parser("upstream-add", help="register or update an upstream server")
    upstream.add_argument("namespace")
    upstream.add_argument("url")
    upstream.add_argument("--connect-timeout-ms", type=int, default=5000)
    upstream.add_argument("--call-timeout-ms", type=int, default=30000)
    upstream.add_argument(
        "--credential-env",
        metavar="NAME",
        help="name of the environment variable holding the service credential to send;"
        " it must end in _SERVICE_TOKEN",
    )
    upstream.set_defaults(handler=_upstream_add)

    policy = commands.add_parser(
        "tool-policy-set",
        help="record whether an upstream tool reads or writes, e.g. tickets__get_ticket read",
    )
    policy.add_argument("tool", help="exposed tool name, <namespace>__<tool>")
    policy.add_argument("effect", choices=["read", "write"])
    policy.add_argument("--notes", default="")
    policy.set_defaults(handler=_tool_policy_set)

    demo = commands.add_parser(
        "seed-demo",
        help="register the ticketing, CRM and handbook upstreams, load tool policies (replacing"
        " each namespace's set exactly), create the demo clients",
    )
    demo.add_argument("--tickets-url", default="http://ticketing:4412/mcp")
    demo.add_argument("--credential-env", default="TICKETING_SERVICE_TOKEN", metavar="NAME")
    demo.add_argument("--crm-url", default="http://crm:4411/mcp")
    demo.add_argument("--crm-credential-env", default="CRM_SERVICE_TOKEN", metavar="NAME")
    demo.add_argument("--handbook-url", default="http://handbook:4410/mcp")
    demo.add_argument("--handbook-credential-env", default="HANDBOOK_SERVICE_TOKEN", metavar="NAME")
    demo.add_argument("--policies", type=Path, default=Path("config/tool_policies.toml"))
    demo.set_defaults(handler=_seed_demo)

    seed = commands.add_parser(
        "seed-test", help="register the echo upstream and two test clients (echo-test-*)"
    )
    seed.add_argument("--echo-url", default="http://echo:8000/mcp")
    seed.set_defaults(handler=_seed_test)
    return parser


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {number}")
    return number


def _verb(status: ClientStatus) -> str:
    return "enable" if status is ClientStatus.ACTIVE else "disable"


async def _migrate(database_url: str, _: argparse.Namespace) -> None:
    applied = await apply_migrations(database_url, MIGRATIONS_PACKAGE)
    print(f"applied migrations: {applied}" if applied else "database is up to date")


async def _telemetry_setup(database_url: str, _: argparse.Namespace) -> None:
    variables = (
        "TELEMETRY_WRITER_DB_PASSWORD",
        "TELEMETRY_READER_DB_PASSWORD",
        "TELEMETRY_PURGER_DB_PASSWORD",
    )
    missing = [name for name in variables if not os.environ.get(name)]
    if missing:
        raise AdminError(f"{', '.join(missing)} is not set (python3 scripts/init_env.py adds it)")
    await setup_telemetry(
        database_url,
        TelemetryPasswords(
            writer=os.environ["TELEMETRY_WRITER_DB_PASSWORD"],
            reader=os.environ["TELEMETRY_READER_DB_PASSWORD"],
            purger=os.environ["TELEMETRY_PURGER_DB_PASSWORD"],
        ),
    )
    print("telemetry schema is set up")


async def _policy_setup(database_url: str, _: argparse.Namespace) -> None:
    variables = (
        "POLICY_GATEWAY_DB_PASSWORD",
        "POLICY_APPROVER_DB_PASSWORD",
        "POLICY_AUDITOR_DB_PASSWORD",
    )
    missing = [name for name in variables if not os.environ.get(name)]
    if missing:
        raise AdminError(f"{', '.join(missing)} is not set (python3 scripts/init_env.py adds it)")
    await setup_policy(
        database_url,
        PolicyPasswords(
            gateway=os.environ["POLICY_GATEWAY_DB_PASSWORD"],
            approver=os.environ["POLICY_APPROVER_DB_PASSWORD"],
            auditor=os.environ["POLICY_AUDITOR_DB_PASSWORD"],
        ),
    )
    print("policy schema is set up")


def _audit_log(database_url: str) -> SQLAuditLog:
    return audit_log_on(open_database(SecretStr(policy_url(database_url))))


async def _approver_add(database_url: str, args: argparse.Namespace) -> None:
    roles = args.role or ["approver"]
    async with await AsyncConnection.connect(policy_url(database_url), autocommit=True) as db:
        try:
            await db.execute(
                "INSERT INTO approvers (id, display_name, roles) VALUES (%s, %s, %s)"
                " ON CONFLICT (id) DO UPDATE SET display_name = EXCLUDED.display_name,"
                " roles = EXCLUDED.roles, active = true",
                (args.id, args.name, roles),
            )
        except CheckViolation:
            raise AdminError(
                "the id must be lowercase letters, digits, . _ -, starting with a letter"
            ) from None
    print(f"approver {args.id} registered with roles {', '.join(roles)}")


async def _approver_list(database_url: str, args: argparse.Namespace) -> None:
    async with await AsyncConnection.connect(policy_url(database_url)) as db:
        cursor = await db.execute(
            "SELECT id, display_name, roles, active FROM approvers ORDER BY id"
        )
        for approver_id, name, roles, active in await cursor.fetchall():
            print(f"{approver_id}  {name}  {','.join(roles)}  {'active' if active else 'inactive'}")


async def _approver_deactivate(database_url: str, args: argparse.Namespace) -> None:
    async with await AsyncConnection.connect(policy_url(database_url), autocommit=True) as db:
        cursor = await db.execute("UPDATE approvers SET active = false WHERE id = %s", (args.id,))
        if cursor.rowcount == 0:
            raise AdminError(f"no approver {args.id!r}")
    print(f"approver {args.id} deactivated")


async def _audit_anchor(database_url: str, args: argparse.Namespace) -> None:
    log = _audit_log(database_url)
    # Never anchor a log that fails what was anchored before: the anchor would make a rewrite look
    # like the truth. (With no anchor file yet, only the chain is checked.)
    try:
        existing = read_anchors(args.file)
    except AnchorFileError:
        if args.file.exists() or args.file.is_symlink():
            raise
        existing = []  # no file yet: the first anchor
    verified = await verify_with_anchors(log, existing)
    anchor = append_anchor(args.file, verified)  # the head that was verified, not a fresh one
    print(f"anchored record {anchor.seq} ({anchor.record_hash[:12]}...) in {args.file}")


async def _audit_verify(database_url: str, args: argparse.Namespace) -> None:
    log = _audit_log(database_url)
    anchors = read_anchors(args.anchors) if args.anchors else []
    head = await verify_with_anchors(log, anchors)
    print(f"ok: {head.seq} records chain correctly and match {len(anchors)} anchors")


async def _client_add(database_url: str, args: argparse.Namespace) -> None:
    async with await AsyncConnection.connect(database_url) as connection:
        client_id = await AdminRegistry(connection).upsert_client(args.name, args.description)
    print(f"client {args.name}: {client_id}")


async def _client_set_status(database_url: str, args: argparse.Namespace) -> None:
    async with await AsyncConnection.connect(database_url) as connection:
        await AdminRegistry(connection).set_client_status(args.name, args.status)
    print(f"client {args.name}: {args.status.value}")


async def _scope_change(database_url: str, args: argparse.Namespace) -> None:
    async with await AsyncConnection.connect(database_url) as connection:
        registry = AdminRegistry(connection)
        client_id = await registry.client_id(args.name)
        if args.verb == "grant":
            await registry.grant_scopes(client_id, args.tools)
        else:
            await registry.revoke_scopes(client_id, args.tools)
    print(f"client {args.name}: {args.verb}ed {', '.join(args.tools)}")


async def _token_issue(database_url: str, args: argparse.Namespace) -> None:
    expires_at = (
        datetime.now(UTC) + timedelta(days=args.expires_in_days)
        if args.expires_in_days is not None
        else None
    )
    async with await AsyncConnection.connect(database_url) as connection:
        registry = AdminRegistry(connection)
        client_id = await registry.client_id(args.client)
        await _require_token_room(registry, client_id, args.client)
        token = await _issue(registry, client_id, args.label, expires_at)
    print(token.plaintext)


async def _token_rotate(database_url: str, args: argparse.Namespace) -> None:
    async with await AsyncConnection.connect(database_url) as connection:
        registry = AdminRegistry(connection)
        client_id = await registry.client_id(args.client)
        await _require_token_room(registry, client_id, args.client)
        token = await _issue(registry, client_id, "rotated", expires_at=None)
        grace_ends = datetime.now(UTC) + timedelta(hours=args.grace_hours)
        expiring = await registry.expire_live_tokens(
            client_id, grace_ends, keep_lookup_id=token.lookup_id
        )
    print(token.plaintext)
    print(f"{expiring} older token(s) expire at {grace_ends.isoformat()}", file=sys.stderr)


async def _token_revoke(database_url: str, args: argparse.Namespace) -> None:
    async with await AsyncConnection.connect(database_url) as connection:
        revoked = await AdminRegistry(connection).revoke_token(args.lookup_id)
    if not revoked:
        raise AdminError(f"no live token with lookup id {args.lookup_id!r}")
    print(f"revoked {args.lookup_id}")


async def _upstream_add(database_url: str, args: argparse.Namespace) -> None:
    _require_credential_env_name(args.credential_env)
    async with await AsyncConnection.connect(database_url) as connection:
        await AdminRegistry(connection).upsert_upstream(
            args.namespace,
            args.url,
            args.connect_timeout_ms,
            args.call_timeout_ms,
            args.credential_env,
        )
    print(f"upstream {args.namespace}: {args.url}")


async def _tool_policy_set(database_url: str, args: argparse.Namespace) -> None:
    split = split_exposed(args.tool)
    if split is None:
        raise AdminError(f"{args.tool!r} is not a <namespace>__<tool> name")
    namespace, tool = split
    async with await AsyncConnection.connect(database_url) as connection:
        await AdminRegistry(connection).upsert_tool_policy(namespace, tool, args.effect, args.notes)
    print(f"{args.tool}: {args.effect}")


async def _seed_test(database_url: str, args: argparse.Namespace) -> None:
    """Idempotent: re-running revokes the previous test tokens and prints new ones."""
    tokens = {}
    async with await AsyncConnection.connect(database_url) as connection:
        registry = AdminRegistry(connection)
        await registry.upsert_upstream(
            TEST_ECHO_NAMESPACE, args.echo_url, connect_timeout_ms=5000, call_timeout_ms=10000
        )
        for name, (description, scopes) in TEST_CLIENTS.items():
            client_id = await registry.upsert_client(name, description)
            await registry.set_scopes(client_id, scopes)
            await registry.revoke_all_tokens(client_id)
            token = await _issue(registry, client_id, "seed-test", expires_at=None)
            tokens[name] = token.plaintext
    print(json.dumps(tokens, indent=2))


def _require_credential_env_name(name: str | None) -> None:
    if name is not None and not name.endswith(CREDENTIAL_ENV_SUFFIX):
        raise AdminError(f"--credential-env must name a variable ending in {CREDENTIAL_ENV_SUFFIX}")


async def _seed_demo(database_url: str, args: argparse.Namespace) -> None:
    """Idempotent: re-running revokes the previous demo tokens and prints new ones."""
    upstreams = [
        (DEMO_TICKETS_NAMESPACE, args.tickets_url, args.credential_env),
        (DEMO_CRM_NAMESPACE, args.crm_url, args.crm_credential_env),
        (DEMO_HANDBOOK_NAMESPACE, args.handbook_url, args.handbook_credential_env),
    ]
    for _, _, credential_env in upstreams:
        _require_credential_env_name(credential_env)
    try:
        policies = load_tool_policies(args.policies)
    except ToolPolicyFileError as error:
        raise AdminError(str(error)) from error
    tokens = {}
    async with await AsyncConnection.connect(database_url) as connection:
        registry = AdminRegistry(connection)
        for namespace, url, credential_env in upstreams:
            await registry.upsert_upstream(
                namespace,
                url,
                connect_timeout_ms=5000,
                call_timeout_ms=10000,
                credential_env=credential_env,
            )
        for namespace in sorted({policy.namespace for policy in policies}):
            await registry.replace_tool_policies(
                namespace,
                [(p.tool, p.effect, p.notes) for p in policies if p.namespace == namespace],
            )
        for name, (description, scopes) in DEMO_CLIENTS.items():
            client_id = await registry.upsert_client(name, description)
            await registry.set_scopes(client_id, scopes)
            await registry.revoke_all_tokens(client_id)
            token = await _issue(registry, client_id, "seed-demo", expires_at=None)
            tokens[name] = token.plaintext
    print(json.dumps(tokens, indent=2))


async def _require_token_room(registry: AdminRegistry, client_id: UUID, name: str) -> None:
    if await registry.count_live_tokens(client_id) >= MAX_LIVE_TOKENS_PER_CLIENT:
        raise AdminError(
            f"client {name} already has {MAX_LIVE_TOKENS_PER_CLIENT} live tokens; revoke one first"
        )


async def _issue(
    registry: AdminRegistry, client_id: UUID, label: str, expires_at: datetime | None
) -> IssuedToken:
    token = generate_token()
    await registry.insert_token(client_id, token.lookup_id, token.token_sha256, label, expires_at)
    print(
        f"Issued token {token.lookup_id}. It is shown once; store it in a secret manager.",
        file=sys.stderr,
    )
    return token
