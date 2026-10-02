"""The echo fixture server answers over streamable HTTP. Needs no database."""

import httpx2
import pytest
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import TextContent


@pytest.mark.anyio
async def test_echo_server_lists_and_calls_tools(echo_url: str) -> None:
    async with httpx2.AsyncClient() as http_client:
        transport = streamable_http_client(echo_url, http_client=http_client)
        async with Client(transport, mode="legacy") as client:
            listing = await client.list_tools()
            assert {tool.name for tool in listing.tools} == {"say", "shout", "wait"}

            result = await client.call_tool("shout", {"text": "harbor"})

    assert result.is_error is False
    content = result.content[0]
    assert isinstance(content, TextContent)
    assert content.text == "HARBOR"
