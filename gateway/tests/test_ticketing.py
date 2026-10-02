"""The ticketing tools, against the seeded fictional database as the ticketing_app role."""

import json
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import psycopg
import pytest
from mcp.client import Client
from mcp.shared.exceptions import MCPError
from mcp.types import INVALID_PARAMS, CallToolResult
from psycopg_pool import AsyncConnectionPool

from mcp_common.attribution import CLIENT_META_KEY
from mcp_common.schema_contract import check_input_schema
from ticketing_server.repo import TicketRepo
from ticketing_server.seed import (
    COMMENT_COUNT,
    INTERNAL_MARKER,
    TICKET_COUNT,
    Dataset,
    build_dataset,
    load_extra_records,
)
from ticketing_server.tools import INSTRUCTIONS, build_toolset

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

READ_TOOLS = {"list_tickets", "get_ticket"}
WRITE_TOOLS = {"create_ticket", "add_comment", "change_status", "assign"}


@pytest.fixture
async def client(ticketing_pool: AsyncConnectionPool) -> AsyncIterator[Client]:
    server = build_toolset(TicketRepo(ticketing_pool)).build_server("ticketing", "0", INSTRUCTIONS)
    async with Client(server, mode="legacy") as connected:
        yield connected


def _structured(result: CallToolResult) -> dict[str, Any]:
    assert not result.is_error, result.content
    assert isinstance(result.structured_content, dict)
    return result.structured_content


async def _rows(url: str, query: str, *params: object) -> list[tuple[Any, ...]]:
    async with await psycopg.AsyncConnection.connect(url) as connection:
        await connection.execute("SET search_path TO ticketing")
        cursor = await connection.execute(query.encode(), params)
        return await cursor.fetchall()


# --- the tool surface ------------------------------------------------------------------


async def test_the_server_offers_six_tools_with_the_right_read_only_hints(client: Client) -> None:
    tools = (await client.list_tools()).tools

    hints = {tool.name: tool.annotations.read_only_hint for tool in tools if tool.annotations}
    assert hints == {name: name in READ_TOOLS for name in READ_TOOLS | WRITE_TOOLS}


async def test_every_input_schema_meets_the_contract(client: Client) -> None:
    for tool in (await client.list_tools()).tools:
        assert check_input_schema(tool.input_schema) == [], tool.name


def test_the_instructions_say_the_data_is_fictional() -> None:
    assert "fictional" in INSTRUCTIONS


# --- reads -------------------------------------------------------------------------------


async def test_list_tickets_defaults_to_ten_newest_activity_first(client: Client) -> None:
    tickets = _structured(await client.call_tool("list_tickets", {}))["tickets"]

    assert len(tickets) == 10
    assert [t["updated_at"] for t in tickets] == sorted(
        (t["updated_at"] for t in tickets), reverse=True
    )


async def test_list_tickets_filters(client: Client, ticketing_data: Dataset) -> None:
    account = ticketing_data.tickets[0].account_id
    expected = {t.id for t in ticketing_data.tickets if t.account_id == account}

    by_account = _structured(
        await client.call_tool("list_tickets", {"account_id": account, "limit": 20})
    )["tickets"]
    urgent = _structured(
        await client.call_tool("list_tickets", {"priority": "urgent", "limit": 20})
    )
    closed = _structured(await client.call_tool("list_tickets", {"status": "closed", "limit": 20}))

    assert {t["id"] for t in by_account} == expected
    assert {t["priority"] for t in urgent["tickets"]} == {"urgent"}
    assert {t["status"] for t in closed["tickets"]} == {"closed"}


@pytest.mark.parametrize("limit", [0, 21, -1])
async def test_list_tickets_limit_is_bounded(client: Client, limit: int) -> None:
    with pytest.raises(MCPError) as error:
        await client.call_tool("list_tickets", {"limit": limit})

    assert error.value.code == INVALID_PARAMS


async def test_get_ticket_returns_the_ticket_with_public_comments_only(
    client: Client, ticketing_data: Dataset
) -> None:
    ticket = next(
        t for t in ticketing_data.tickets
        if any(c.ticket_id == t.id and c.visibility == "internal" for c in ticketing_data.comments)
    )  # fmt: skip
    public = [
        c.body
        for c in ticketing_data.comments
        if c.ticket_id == ticket.id and c.visibility == "public"
    ]

    detail = _structured(await client.call_tool("get_ticket", {"ticket_id": ticket.id}))

    assert detail["id"] == ticket.id
    assert detail["description"] == ticket.description
    assert [c["body"] for c in detail["comments"]] == public
    assert detail["comments_total"] == len(public)
    assert "internal_notes" not in detail


async def test_get_ticket_returns_only_the_twenty_newest_public_comments(
    client: Client, test_database_url: str
) -> None:
    before = await _rows(
        test_database_url,
        "SELECT count(*) FROM comments WHERE ticket_id = 'TKT-000001' AND visibility = 'public'",
    )
    async with await psycopg.AsyncConnection.connect(test_database_url) as connection:
        await connection.execute("SET search_path TO ticketing")
        for number in range(1, 26):
            visibility = "internal" if number % 5 == 0 else "public"
            await connection.execute(
                "INSERT INTO comments (ticket_id, author, visibility, body, requested_by)"
                " VALUES ('TKT-000001', 'a.b', %s, %s, 'direct')",
                (visibility, f"bulk comment {number}"),
            )

    detail = _structured(await client.call_tool("get_ticket", {"ticket_id": "TKT-000001"}))

    bodies = [c["body"] for c in detail["comments"]]
    assert len(bodies) == 20
    assert detail["comments_total"] == before[0][0] + 20  # 25 inserted, 5 of them internal
    assert bodies[-1] == "bulk comment 24"
    assert not any(body == "bulk comment 25" for body in bodies)  # 25 is internal
    ids = [c["id"] for c in detail["comments"]]
    assert ids == sorted(ids)


async def test_get_ticket_for_an_unknown_id_is_a_tool_error(client: Client) -> None:
    result = await client.call_tool("get_ticket", {"ticket_id": "TKT-999999"})

    assert result.is_error


async def test_no_read_tool_ever_returns_an_internal_value(
    client: Client, ticketing_data: Dataset
) -> None:
    internal = ticketing_data.internal_values()
    assert len(internal) > 20  # the seed really does contain internal text to leak
    outputs = []
    for ticket in ticketing_data.tickets:
        result = await client.call_tool("get_ticket", {"ticket_id": ticket.id})
        outputs.append(json.dumps(result.structured_content))
        outputs.extend(c.model_dump_json() for c in result.content)
    for number in range(1, 41):
        result = await client.call_tool(
            "list_tickets", {"account_id": f"ACC-{number:05d}", "limit": 20}
        )
        outputs.append(json.dumps(result.structured_content))
    everything = "\n".join(outputs)

    assert INTERNAL_MARKER not in everything
    assert [value for value in internal if value in everything] == []


# --- writes ------------------------------------------------------------------------------


async def test_create_ticket_returns_the_next_id_and_records_who_asked(
    client: Client, ticketing_app_url: str
) -> None:
    result = await client.call_tool(
        "create_ticket",
        {"subject": "Dock gate stuck", "description": "The gate will not latch.",
         "priority": "high", "account_id": "ACC-00007"},
        meta=cast(Any, {CLIENT_META_KEY: "harborline-ops-bot"}),
    )  # fmt: skip

    ticket_id = _structured(result)["ticket_id"]
    assert ticket_id == f"TKT-{TICKET_COUNT + 1:06d}"
    rows = await _rows(
        ticketing_app_url,
        "SELECT requested_by, priority, status, assignee FROM tickets WHERE id = %s",
        ticket_id,
    )
    assert rows == [("harborline-ops-bot", "high", "open", None)]


async def test_a_call_without_a_client_name_is_recorded_as_direct(
    client: Client, ticketing_app_url: str
) -> None:
    result = await client.call_tool(
        "create_ticket",
        {"subject": "Dock gate stuck", "description": "x", "priority": "low",
         "account_id": "ACC-00001"},
    )  # fmt: skip

    rows = await _rows(
        ticketing_app_url,
        "SELECT requested_by FROM tickets WHERE id = %s",
        _structured(result)["ticket_id"],
    )
    assert rows == [("direct",)]


@pytest.mark.parametrize("bad_name", ["Not A Name", "x" * 65, "UPPER"])
async def test_an_invalid_client_name_in_meta_is_not_stored(
    client: Client, ticketing_app_url: str, bad_name: str
) -> None:
    result = await client.call_tool(
        "create_ticket",
        {"subject": "Dock gate stuck", "description": "x", "priority": "low",
         "account_id": "ACC-00001"},
        meta=cast(Any, {CLIENT_META_KEY: bad_name}),
    )  # fmt: skip

    rows = await _rows(
        ticketing_app_url,
        "SELECT requested_by FROM tickets WHERE id = %s",
        _structured(result)["ticket_id"],
    )
    assert rows == [("direct",)]


async def test_add_comment_is_always_public(client: Client, ticketing_app_url: str) -> None:
    result = await client.call_tool(
        "add_comment",
        {"ticket_id": "TKT-000001", "body": "We shipped a replacement."},
        meta=cast(Any, {CLIENT_META_KEY: "harborline-support-bot"}),
    )

    comment_id = _structured(result)["comment_id"]
    rows = await _rows(
        ticketing_app_url,
        "SELECT visibility, requested_by, body FROM comments WHERE id = %s",
        comment_id,
    )
    assert rows == [("public", "harborline-support-bot", "We shipped a replacement.")]
    detail = _structured(await client.call_tool("get_ticket", {"ticket_id": "TKT-000001"}))
    assert detail["comments"][-1]["body"] == "We shipped a replacement."


async def test_add_comment_to_an_unknown_ticket_is_a_tool_error(client: Client) -> None:
    result = await client.call_tool("add_comment", {"ticket_id": "TKT-999999", "body": "hello"})

    assert result.is_error


async def test_change_status(client: Client, ticketing_app_url: str) -> None:
    result = await client.call_tool(
        "change_status", {"ticket_id": "TKT-000002", "status": "resolved"}
    )

    assert _structured(result) == {"ticket_id": "TKT-000002", "status": "resolved"}
    rows = await _rows(ticketing_app_url, "SELECT status FROM tickets WHERE id = 'TKT-000002'")
    assert rows == [("resolved",)]


async def test_change_status_of_an_unknown_ticket_is_a_tool_error(client: Client) -> None:
    result = await client.call_tool(
        "change_status", {"ticket_id": "TKT-999999", "status": "closed"}
    )

    assert result.is_error


async def test_assign(client: Client, ticketing_data: Dataset) -> None:
    handle = ticketing_data.staff[0].handle

    result = await client.call_tool("assign", {"ticket_id": "TKT-000003", "assignee": handle})

    assert _structured(result) == {"ticket_id": "TKT-000003", "assignee": handle}


@pytest.mark.parametrize(
    "arguments",
    [
        {"ticket_id": "TKT-000003", "assignee": "nobody.here"},
        {"ticket_id": "TKT-999999", "assignee": "nobody.here"},
    ],
)
async def test_assign_to_an_unknown_handle_or_ticket_is_a_tool_error(
    client: Client, arguments: dict[str, str]
) -> None:
    result = await client.call_tool("assign", arguments)

    assert result.is_error


# --- strict inputs -------------------------------------------------------------------------

VALID_ARGUMENTS: dict[str, dict[str, Any]] = {
    "list_tickets": {},
    "get_ticket": {"ticket_id": "TKT-000001"},
    "create_ticket": {"subject": "abc", "description": "d", "account_id": "ACC-00001"},
    "add_comment": {"ticket_id": "TKT-000001", "body": "b"},
    "change_status": {"ticket_id": "TKT-000001", "status": "open"},
    "assign": {"ticket_id": "TKT-000001", "assignee": "a.b"},
}


@pytest.mark.parametrize("tool", sorted(VALID_ARGUMENTS))
async def test_an_unknown_argument_is_refused_before_anything_runs(
    client: Client, tool: str
) -> None:
    with pytest.raises(MCPError) as error:
        await client.call_tool(tool, {**VALID_ARGUMENTS[tool], "internal_notes": "x"})

    assert error.value.code == INVALID_PARAMS


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("get_ticket", {"ticket_id": "TKT-1"}),
        ("get_ticket", {"ticket_id": "TKT-000001 OR 1=1"}),
        ("create_ticket", {"subject": "ab", "description": "d", "account_id": "ACC-00001"}),
        ("create_ticket", {"subject": "abc", "description": "d" * 4001, "account_id": "ACC-00001"}),
        ("create_ticket", {"subject": "abc", "description": "d", "account_id": "ACC-1"}),
        ("create_ticket", {"subject": "abc", "description": "d", "account_id": "ACC-00001",
                           "priority": "critical"}),
        ("add_comment", {"ticket_id": "TKT-000001", "body": ""}),
        ("add_comment", {"ticket_id": "TKT-000001", "body": "b" * 2001}),
        ("change_status", {"ticket_id": "TKT-000001", "status": "deleted"}),
        ("assign", {"ticket_id": "TKT-000001", "assignee": "Not.Valid"}),
        ("assign", {"ticket_id": "TKT-000001", "assignee": "a.b'; DROP TABLE tickets;--"}),
    ],
)  # fmt: skip
async def test_out_of_bounds_values_are_refused(
    client: Client, tool: str, arguments: dict[str, Any]
) -> None:
    with pytest.raises(MCPError) as error:
        await client.call_tool(tool, arguments)

    assert error.value.code == INVALID_PARAMS


# --- the seed --------------------------------------------------------------------------------


def test_the_seed_is_deterministic() -> None:
    assert build_dataset() == build_dataset()
    assert build_dataset(seed=1) != build_dataset()


def test_the_seed_has_the_documented_shape() -> None:
    dataset = build_dataset()
    internal_comments = [c for c in dataset.comments if c.visibility == "internal"]
    with_notes = [t for t in dataset.tickets if t.internal_notes]

    assert (len(dataset.staff), len(dataset.tickets), len(dataset.comments)) == (
        12, TICKET_COUNT, COMMENT_COUNT,
    )  # fmt: skip
    assert 0.1 < len(internal_comments) / COMMENT_COUNT < 0.3
    assert 0.15 < len(with_notes) / TICKET_COUNT < 0.45
    assert all(value.startswith(INTERNAL_MARKER) for value in dataset.internal_values())
    assert {t.account_id for t in dataset.tickets} <= {f"ACC-{n:05d}" for n in range(1, 41)}


def test_the_seed_contains_no_real_looking_contact_data() -> None:
    text = json.dumps([c.body for c in build_dataset().comments])

    assert all(domain.endswith(".example") for domain in re.findall(r"@([\w.-]+)", text))
    assert all(phone.startswith("555-01") for phone in re.findall(r"\b\d{3}-\d{4}\b", text))


def test_the_seed_in_the_database_is_the_seed_in_code(
    ticketing_app_url: str, ticketing_data: Dataset
) -> None:
    import anyio

    rows = anyio.run(
        _rows, ticketing_app_url, "SELECT id, subject, internal_notes FROM tickets ORDER BY id"
    )

    assert rows == [(t.id, t.subject, t.internal_notes) for t in ticketing_data.tickets]


def test_extra_records_are_merged_into_the_seed(tmp_path: Path) -> None:
    extra = tmp_path / "extra.json"
    extra.write_text(
        json.dumps(
            {
                "tickets": [
                    {"account_id": "ACC-00001", "subject": "Planted", "description": "text"}
                ],
                "comments": [{"ticket_id": "TKT-000001", "author": "x.y", "body": "planted"}],
            }
        )
    )
    dataset = build_dataset()

    load_extra_records(dataset, extra)

    assert dataset.tickets[-1].id == f"TKT-{TICKET_COUNT + 1:06d}"
    assert dataset.comments[-1].body == "planted"


def test_extra_records_reject_unknown_fields(tmp_path: Path) -> None:
    extra = tmp_path / "extra.json"
    extra.write_text(json.dumps({"tickets": [], "surprise": 1}))

    with pytest.raises(ValueError, match="surprise"):
        load_extra_records(build_dataset(), extra)
