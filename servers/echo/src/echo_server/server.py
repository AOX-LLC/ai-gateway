"""Test fixture, not one of the product's MCP servers.

A minimal MCP server with three tools, used to exercise the gateway end to end in tests.
`wait` exists to test cancellation: it sleeps, and counts the times it was cancelled
mid-sleep, so a test running the server in-process can see whether a cancellation
reached it.
"""

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette


class CancellationCounter:
    """How many `wait` calls were cancelled while sleeping. In-process, for tests."""

    def __init__(self) -> None:
        self.count = 0
        self.started = 0


cancellations = CancellationCounter()


def build_server() -> MCPServer:
    server = MCPServer("echo")

    @server.tool(description="Return the text unchanged.")
    def say(text: str) -> str:
        return text

    @server.tool(description="Return the text in upper case.")
    def shout(text: str) -> str:
        return text.upper()

    @server.tool(description="Sleep for the given number of seconds, then return 'done'.")
    async def wait(seconds: float) -> str:
        cancellations.started += 1
        try:
            await anyio.sleep(seconds)
        except anyio.get_cancelled_exc_class():
            cancellations.count += 1
            raise
        return "done"

    return server


def build_app(allowed_hosts: list[str]) -> Starlette:
    """The server's streamable HTTP app at /mcp, with DNS-rebinding protection on."""
    return build_server().streamable_http_app(
        streamable_http_path="/mcp",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=allowed_hosts
        ),
    )
