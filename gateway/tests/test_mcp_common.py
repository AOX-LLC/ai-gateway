"""The shared MCP server building blocks: strict tools, the service credential, the contract."""

import logging
from typing import Any, Literal, cast

import httpx2
import pytest
from mcp.client import Client
from mcp.shared.exceptions import MCPError
from mcp.types import INVALID_PARAMS, TextContent
from pydantic import BaseModel, ConfigDict, Field
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from mcp_common.attribution import CLIENT_META_KEY, client_name_from_meta
from mcp_common.credentials import (
    MissingCredentialError,
    ServiceCredentialMiddleware,
    credential_from_env,
)
from mcp_common.schema_contract import check_input_schema
from mcp_common.toolset import CallInfo, StrictToolset, ToolError

pytestmark = pytest.mark.anyio


class GreetInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=20)
    mood: Literal["calm", "loud"] = "calm"


class GreetOutput(BaseModel):
    greeting: str
    asked_by: str


async def greet(arguments: GreetInput, info: CallInfo) -> GreetOutput:
    if arguments.name == "nobody":
        raise ToolError("There is nobody to greet.")
    if arguments.name == "boom":
        raise RuntimeError("secret internal detail")
    return GreetOutput(greeting=f"hello {arguments.name}", asked_by=info.requested_by)


def _toolset() -> StrictToolset:
    toolset = StrictToolset()
    toolset.register("greet", "Greet someone.", GreetInput, GreetOutput, greet, read_only=True)
    return toolset


@pytest.fixture
async def client() -> Any:
    server = _toolset().build_server("test", "0", "Fictional test server.")
    async with Client(server, mode="legacy") as connected:
        yield connected


async def test_the_listed_tool_has_a_closed_schema_and_a_read_only_hint(client: Client) -> None:
    [tool] = (await client.list_tools()).tools

    assert tool.input_schema["additionalProperties"] is False
    assert tool.output_schema is not None
    assert tool.annotations is not None
    assert tool.annotations.read_only_hint is True
    assert check_input_schema(tool.input_schema) == []


async def test_a_call_returns_structured_content_and_a_text_rendering(client: Client) -> None:
    result = await client.call_tool("greet", {"name": "Marta"})

    assert result.structured_content == {"greeting": "hello Marta", "asked_by": "direct"}
    [text] = result.content
    assert isinstance(text, TextContent)
    assert "hello Marta" in text.text


async def test_the_client_name_in_meta_is_passed_to_the_handler(client: Client) -> None:
    result = await client.call_tool(
        "greet", {"name": "Marta"}, meta=cast(Any, {CLIENT_META_KEY: "harborline-ops-bot"})
    )

    assert result.structured_content is not None
    assert result.structured_content["asked_by"] == "harborline-ops-bot"


@pytest.mark.parametrize(
    "arguments",
    [{"name": "Marta", "extra": 1}, {}, {"name": ""}, {"name": "x" * 21}, {"name": 5}],
    ids=["extra", "missing", "too-short", "too-long", "wrong-type"],
)
async def test_invalid_arguments_are_a_short_protocol_error(
    client: Client, arguments: dict[str, Any]
) -> None:
    with pytest.raises(MCPError) as error:
        await client.call_tool("greet", arguments)

    assert error.value.code == INVALID_PARAMS
    assert error.value.message == "Invalid arguments for tool 'greet'."


async def test_an_unknown_tool_is_a_protocol_error(client: Client) -> None:
    with pytest.raises(MCPError) as error:
        await client.call_tool("nothing", {})

    assert error.value.code == INVALID_PARAMS


async def test_a_tool_error_is_a_result_the_model_can_read(client: Client) -> None:
    result = await client.call_tool("greet", {"name": "nobody"})

    assert result.is_error
    assert result.content == [TextContent(type="text", text="There is nobody to greet.")]


async def test_an_unexpected_exception_never_leaks_its_text(
    client: Client, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.ERROR)

    with pytest.raises(MCPError) as error:
        await client.call_tool("greet", {"name": "boom"})

    assert "secret internal detail" not in error.value.message


def test_a_model_that_allows_extra_fields_cannot_be_registered() -> None:
    class Loose(BaseModel):
        name: str

    with pytest.raises(ValueError, match="extra='forbid'"):
        StrictToolset().register("loose", "x", Loose, GreetOutput, greet, read_only=True)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("meta", "expected"),
    [
        (None, "direct"),
        ({}, "direct"),
        ({CLIENT_META_KEY: "harborline-ops-bot"}, "harborline-ops-bot"),
        ({CLIENT_META_KEY: 7}, "direct"),
        ({CLIENT_META_KEY: "Has Spaces"}, "direct"),
        ({CLIENT_META_KEY: "a" * 65}, "direct"),
        ({CLIENT_META_KEY: "x"}, "direct"),
    ],
)
def test_the_client_name_is_validated(meta: dict[str, Any] | None, expected: str) -> None:
    assert client_name_from_meta(meta) == expected


# --- service credential ----------------------------------------------------------------


async def _ok(_: Request) -> PlainTextResponse:
    return PlainTextResponse("reached")


def _protected(credential: str) -> httpx2.AsyncClient:
    app = Starlette(routes=[Route("/", _ok)])
    wrapped = ServiceCredentialMiddleware(app, credential)
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=wrapped), base_url="http://test")


async def test_the_right_credential_reaches_the_app() -> None:
    async with _protected("s3cret-value") as http:
        response = await http.get("/", headers={"Authorization": "Bearer s3cret-value"})

    assert (response.status_code, response.text) == (200, "reached")


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong"},
        {"Authorization": "Bearer s3cret-valu"},
        {"Authorization": "Bearer s3cret-value-and-more"},
        {"Authorization": "Basic czNjcmV0LXZhbHVl"},
        {"Authorization": "s3cret-value"},
        {"Authorization": "Bearer "},
    ],
)
async def test_any_other_request_gets_the_same_bare_401(headers: dict[str, str]) -> None:
    async with _protected("s3cret-value") as http:
        response = await http.get("/", headers=headers)

    assert response.status_code == 401
    assert response.json() == {"error": "unauthorized"}


@pytest.mark.parametrize("credential", ["", "   "])
def test_an_empty_credential_refuses_to_start(credential: str) -> None:
    with pytest.raises(MissingCredentialError):
        ServiceCredentialMiddleware(Starlette(), credential)


def test_the_credential_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEST_SERVICE_TOKEN", raising=False)
    with pytest.raises(MissingCredentialError):
        credential_from_env("TEST_SERVICE_TOKEN")

    monkeypatch.setenv("TEST_SERVICE_TOKEN", "")
    with pytest.raises(MissingCredentialError):
        credential_from_env("TEST_SERVICE_TOKEN")

    monkeypatch.setenv("TEST_SERVICE_TOKEN", "value")
    assert credential_from_env("TEST_SERVICE_TOKEN") == "value"


# --- schema contract ---------------------------------------------------------------------

_GOOD = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "id": {"type": "string", "pattern": "^A-[0-9]{3}$"},
        "kind": {"anyOf": [{"type": "string", "enum": ["a", "b"]}, {"type": "null"}]},
        "limit": {"type": "integer", "minimum": 1, "maximum": 20},
        "tags": {"type": "array", "maxItems": 3, "items": {"type": "string", "maxLength": 8}},
    },
}


def test_a_closed_bounded_schema_meets_the_contract() -> None:
    assert check_input_schema(_GOOD) == []


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"additionalProperties": True}, "additionalProperties"),
        ({"properties": {"s": {"type": "string"}}}, "string needs"),
        ({"properties": {"n": {"type": "integer", "minimum": 0}}}, "number needs"),
        ({"properties": {"n": {"type": "integer"}}}, "number needs"),
        (
            {"properties": {"a": {"type": "array", "items": {"type": "string", "maxLength": 1}}}},
            "maxItems",
        ),
        ({"properties": {"o": {"type": "object", "additionalProperties": False}}}, "nested"),
        ({"properties": {"r": {"$ref": "#/$defs/x"}}}, "$ref"),
        ({"properties": {"u": {"anyOf": [{"type": "string"}, {"type": "null"}]}}}, "string needs"),
    ],
)
def test_each_violation_is_reported(change: dict[str, Any], expected: str) -> None:
    problems = check_input_schema({**_GOOD, **change})

    assert any(expected in problem for problem in problems), problems
