"""Test fixture, not one of the product's MCP servers.

A minimal MCP server with two tools, used to exercise the gateway end to end in tests.
"""

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette


def build_server() -> MCPServer:
    server = MCPServer("echo")

    @server.tool(description="Return the text unchanged.")
    def say(text: str) -> str:
        return text

    @server.tool(description="Return the text in upper case.")
    def shout(text: str) -> str:
        return text.upper()

    return server


def build_app(allowed_hosts: list[str]) -> Starlette:
    """The server's streamable HTTP app at /mcp, with DNS-rebinding protection on."""
    return build_server().streamable_http_app(
        streamable_http_path="/mcp",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=allowed_hosts
        ),
    )
