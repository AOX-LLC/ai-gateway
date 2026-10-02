"""Acceptance check: authenticate to the gateway and confirm what this client may do.

    GATEWAY_TOKEN=... uv run scripts/test_client.py --expect echo__say --refused echo__shout
    GATEWAY_TOKEN=<revoked token> uv run scripts/test_client.py --expect-rejected

Passes (exit 0) only when tools/list returns exactly the --expect tools, each of them can
be called, each --refused tool is refused, and requests without a valid token get 401.
The token is read from the environment so it never appears in shell history or argv.
"""

import argparse
import os
import sys
from collections.abc import Sequence

import anyio
import httpx2
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp.types import INVALID_PARAMS

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


class CheckFailedError(Exception):
    pass


def main(argv: Sequence[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    token = os.environ.get("GATEWAY_TOKEN", "")
    if not token:
        sys.exit("test_client: set GATEWAY_TOKEN")
    failures: list[BaseException] = []
    try:
        anyio.run(_run_checks, args, token)
    except* CheckFailedError as group:
        # A failure inside the MCP client's context arrives wrapped in an exception group.
        failures.extend(group.exceptions)
    for failure in failures:
        print(f"FAIL  {failure}")
    if failures:
        sys.exit(1)
    print("PASS  all checks")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check what a gateway client may do.")
    parser.add_argument("--url", default="http://127.0.0.1:4401/mcp")
    parser.add_argument("--expect", action="append", default=[], metavar="TOOL")
    parser.add_argument("--refused", action="append", default=[], metavar="TOOL")
    parser.add_argument(
        "--expect-rejected",
        action="store_true",
        help="only check that GATEWAY_TOKEN itself is refused with 401",
    )
    return parser


async def _run_checks(args: argparse.Namespace, token: str) -> None:
    if args.expect_rejected:
        await _check_status(args.url, token, 401, "the given token is refused")
        return

    await _check_status(args.url, None, 401, "a request without a token is refused")
    await _check_status(args.url, "aig_notatoken", 401, "a malformed token is refused")

    async with (
        httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http_client,
        Client(streamable_http_client(args.url, http_client=http_client), mode="legacy") as client,
    ):
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
