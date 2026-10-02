"""Acceptance check: authenticate to the gateway and confirm what this client may do.

    GATEWAY_TOKEN=... uv run scripts/test_client.py --expect echo__say --refused echo__shout
    GATEWAY_TOKEN=<revoked token> uv run scripts/test_client.py --expect-rejected
    GATEWAY_TOKEN=... uv run scripts/test_client.py \\
        --scenario scripts/scenarios/harborline.toml --as harborline-support-bot
    TICKETING_SERVICE_TOKEN=... uv run scripts/test_client.py \\
        --scenario scripts/scenarios/harborline.toml --direct ticketing   # or crm, handbook

Passes (exit 0) only when tools/list returns exactly the --expect tools, each of them can
be called, each --refused tool is refused, and requests without a valid token get 401.

A scenario file lists one call per tool, with the tool's namespace, sample arguments, an
expected text and the clients allowed to make it. Through the gateway (--as names the client
that owns GATEWAY_TOKEN), tools/list must return exactly the allowed tools of every
namespace, every allowed call must succeed and every other call must be refused with
-32602. With --direct <server>, that server is called without the gateway, using its own
service credential (TICKETING_, CRM_ or HANDBOOK_SERVICE_TOKEN); its tools/list must return
exactly the scenario's tools for it, every call must succeed, and requests without the
credential must get 401.

Tokens are read from the environment so they never appear in shell history or argv.
"""

import argparse
import os
import sys
import tomllib
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
import httpx2
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp.types import INVALID_PARAMS, CallToolResult, TextContent

_INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-11-25",
        "capabilities": {},
        "clientInfo": {"name": "test-client", "version": "0"},
    },
}
_SAMPLE_TEXT = "Harborline (fictional) test message"


@dataclass(frozen=True)
class DirectServer:
    namespace: str
    """The namespace the gateway gives the server's tools; scenario calls name it."""
    url: str
    credential_env: str


_DIRECT = {
    "ticketing": DirectServer("tickets", "http://127.0.0.1:4412/mcp", "TICKETING_SERVICE_TOKEN"),
    "crm": DirectServer("crm", "http://127.0.0.1:4411/mcp", "CRM_SERVICE_TOKEN"),
    "handbook": DirectServer("handbook", "http://127.0.0.1:4410/mcp", "HANDBOOK_SERVICE_TOKEN"),
}


@dataclass(frozen=True)
class ScenarioCall:
    namespace: str
    tool: str
    arguments: dict[str, Any]
    expect_text: str
    allowed_for: frozenset[str]


@dataclass(frozen=True)
class Scenario:
    calls: list[ScenarioCall]

    def exposed_name(self, call: ScenarioCall) -> str:
        return f"{call.namespace}__{call.tool}"


class CheckFailedError(Exception):
    pass


def main(argv: Sequence[str] | None = None) -> None:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.direct and not args.scenario:
        parser.error("--direct needs --scenario")
    if args.as_client and not args.scenario:
        parser.error("--as needs --scenario")
    if args.scenario and not args.direct and not args.as_client:
        parser.error("--scenario needs --as <client> or --direct <server>")
    token_env = _DIRECT[args.direct].credential_env if args.direct else "GATEWAY_TOKEN"
    token = os.environ.get(token_env, "")
    if not token:
        sys.exit(f"test_client: set {token_env}")
    failures: list[BaseException] = []
    try:
        anyio.run(_run_checks, args, token)
    except* CheckFailedError as group:
        # A failure inside the MCP client's context arrives wrapped in exception groups.
        failures.extend(_leaves(group))
    for failure in failures:
        print(f"FAIL  {failure}")
    if failures:
        sys.exit(1)
    print("PASS  all checks")


def _leaves(error: BaseException) -> list[BaseException]:
    if isinstance(error, BaseExceptionGroup):
        return [leaf for inner in error.exceptions for leaf in _leaves(inner)]
    return [error]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check what a gateway client may do.")
    parser.add_argument("--url", help="default: the gateway, or the server given to --direct")
    parser.add_argument("--expect", action="append", default=[], metavar="TOOL")
    parser.add_argument("--refused", action="append", default=[], metavar="TOOL")
    parser.add_argument(
        "--expect-rejected",
        action="store_true",
        help="only check that GATEWAY_TOKEN itself is refused with 401",
    )
    parser.add_argument("--scenario", type=Path, help="TOML scenario: one call per tool")
    parser.add_argument("--as", dest="as_client", metavar="CLIENT", help="with --scenario")
    parser.add_argument("--direct", choices=sorted(_DIRECT), help="skip the gateway")
    return parser


async def _run_checks(args: argparse.Namespace, token: str) -> None:
    if args.direct:
        server = _DIRECT[args.direct]
        await _run_direct(args.url or server.url, token, server, _load_scenario(args.scenario))
        return
    url = args.url or "http://127.0.0.1:4401/mcp"
    if args.expect_rejected:
        await _check_status(url, token, 401, "the given token is refused")
        return

    await _check_status(url, None, 401, "a request without a token is refused")
    await _check_status(url, "aig_notatoken", 401, "a malformed token is refused")

    if args.scenario:
        await _run_gateway_scenario(url, token, args.as_client, _load_scenario(args.scenario))
        return

    async with _connect(url, token) as client:
        _ok(f"connected; negotiated MCP protocol {client.protocol_version}")
        listed = sorted(tool.name for tool in (await client.list_tools()).tools)
        if listed != sorted(args.expect):
            raise CheckFailedError(f"tools/list returned {listed}, expected {sorted(args.expect)}")
        _ok(f"tools/list returned exactly {listed}")

        for tool in args.expect:
            result = await client.call_tool(tool, {"text": _SAMPLE_TEXT})
            if result.is_error:
                raise CheckFailedError(f"calling {tool} returned a tool error: {result.content}")
            _ok(f"{tool} answered")

        for tool in args.refused:
            try:
                await client.call_tool(tool, {"text": _SAMPLE_TEXT})
            except MCPError as error:
                if error.code != INVALID_PARAMS:
                    raise CheckFailedError(f"{tool} failed with code {error.code}") from error
                _ok(f"{tool} refused: {error.message}")
            else:
                raise CheckFailedError(f"{tool} was not refused")


@asynccontextmanager
async def _connect(url: str, token: str) -> AsyncGenerator[Client]:
    async with (
        httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http_client,
        Client(streamable_http_client(url, http_client=http_client), mode="legacy") as client,
    ):
        yield client


def _load_scenario(path: Path) -> Scenario:
    document = tomllib.loads(path.read_text(encoding="utf-8"))
    calls = [
        ScenarioCall(
            namespace=entry["namespace"],
            tool=entry["tool"],
            arguments=entry["arguments"],
            expect_text=entry["expect_text"],
            allowed_for=frozenset(entry["allowed_for"]),
        )
        for entry in document["call"]
    ]
    return Scenario(calls=calls)


def _result_text(result: CallToolResult) -> str:
    return "\n".join(block.text for block in result.content if isinstance(block, TextContent))


async def _expect_success(client: Client, name: str, call: ScenarioCall) -> None:
    result = await client.call_tool(name, call.arguments)
    if result.is_error:
        raise CheckFailedError(f"{name} returned a tool error: {_result_text(result)}")
    if call.expect_text not in _result_text(result):
        raise CheckFailedError(f"{name} answered without the expected text {call.expect_text!r}")
    _ok(f"{name} answered as expected")


async def _run_gateway_scenario(url: str, token: str, client_name: str, scenario: Scenario) -> None:
    allowed = {scenario.exposed_name(c) for c in scenario.calls if client_name in c.allowed_for}
    async with _connect(url, token) as client:
        _ok(f"connected as {client_name}; negotiated MCP protocol {client.protocol_version}")
        listed = {t.name for t in (await client.list_tools()).tools}
        if listed != allowed:
            raise CheckFailedError(
                f"tools/list returned {sorted(listed)}, expected {sorted(allowed)}"
            )
        _ok(f"tools/list returned exactly the {len(allowed)} allowed tools")

        for call in scenario.calls:
            name = scenario.exposed_name(call)
            if name in allowed:
                await _expect_success(client, name, call)
                continue
            await _expect_refused(client, name, call.arguments)


async def _expect_refused(client: Client, name: str, arguments: dict[str, Any]) -> None:
    try:
        await client.call_tool(name, arguments)
    except MCPError as error:
        if error.code != INVALID_PARAMS:
            raise CheckFailedError(f"{name} failed with code {error.code}") from error
        _ok(f"{name} refused: {error.message}")
    else:
        raise CheckFailedError(f"{name} was not refused")


async def _run_direct(url: str, credential: str, server: DirectServer, scenario: Scenario) -> None:
    calls = [call for call in scenario.calls if call.namespace == server.namespace]
    if not calls:
        raise CheckFailedError(f"the scenario has no calls for namespace {server.namespace!r}")
    await _check_status(url, None, 401, "a request without the service credential is refused")
    await _check_status(url, "not-the-credential", 401, "a wrong service credential is refused")
    async with _connect(url, credential) as client:
        _ok(f"connected directly; negotiated MCP protocol {client.protocol_version}")
        listed = sorted(tool.name for tool in (await client.list_tools()).tools)
        expected = sorted(call.tool for call in calls)
        if listed != expected:
            raise CheckFailedError(f"tools/list returned {listed}, expected {expected}")
        _ok(f"tools/list returned exactly the {len(listed)} scenario tools")
        for call in calls:
            await _expect_success(client, call.tool, call)
        first = calls[0]
        await _expect_refused(client, first.tool, {**first.arguments, "unexpected": 1})


async def _check_status(url: str, token: str | None, expected: int, description: str) -> None:
    headers = {"Accept": "application/json, text/event-stream"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    async with httpx2.AsyncClient() as http_client:
        response = await http_client.post(url, json=_INITIALIZE, headers=headers)
    if response.status_code != expected:
        raise CheckFailedError(f"{description}: got HTTP {response.status_code}")
    _ok(f"{description} (HTTP {expected})")


def _ok(message: str) -> None:
    print(f"ok    {message}")


if __name__ == "__main__":
    main()
