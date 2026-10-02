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
from uuid import UUID

import anyio
from psycopg import AsyncConnection

from ai_gateway.auth.tokens import IssuedToken, generate_token
from ai_gateway.proxy.naming import split_exposed
from ai_gateway.registry import MIGRATIONS_PACKAGE
from ai_gateway.registry.models import ClientStatus
from ai_gateway.registry.repo import AdminRegistry, ClientNotFoundError
from mcp_common.migrate import apply_migrations

_DATABASE_URL_ENV = "GATEWAY_MIGRATE_DATABASE_URL"
MAX_LIVE_TOKENS_PER_CLIENT = 2

# Fictional demo data for the test profile. Harborline Supply Co. does not exist.
TEST_ECHO_NAMESPACE = "echo"
TEST_CLIENTS = {
    "harborline-support-bot": (
        "Support assistant for the fictional Harborline Supply Co. (test data)",
        ["echo__say"],
    ),
    "harborline-ops-bot": (
        "Operations assistant for the fictional Harborline Supply Co. (test data)",
        ["echo__say", "echo__shout"],
    ),
}


class AdminError(Exception):
    """An operator mistake, reported as one line without a traceback."""


def main(argv: Sequence[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    database_url = os.environ.get(_DATABASE_URL_ENV)
    if not database_url:
        sys.exit(f"gateway-admin: {_DATABASE_URL_ENV} is not set")
    try:
        anyio.run(args.handler, database_url, args)
    except (AdminError, ClientNotFoundError) as error:
        sys.exit(f"gateway-admin: {error}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gateway-admin", description=__doc__)
    commands = parser.add_subparsers(required=True, metavar="command")

    migrate = commands.add_parser("migrate", help="apply database migrations")
    migrate.set_defaults(handler=_migrate)

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
        help="name of the environment variable holding the service credential to send",
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

    seed = commands.add_parser(
        "seed-test", help="register the echo upstream and two fictional test clients"
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
            await registry.grant_scopes(client_id, scopes)
            await registry.revoke_all_tokens(client_id)
            token = await _issue(registry, client_id, "seed-test", expires_at=None)
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
