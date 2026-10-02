"""Every read tool of all three servers, scanned for every seeded internal value.

The servers are queried in-process against the seeded database. The values that must never
appear are the CRM's internal columns, the ticketing internal notes and comments, and the
text of every restricted handbook document and chunk. A value counts as leaked if it appears
anywhere in a tool's result, in the structured data or in the text a model would read.
"""

import json
import re
from collections.abc import AsyncIterator

import pytest
from mcp.client import Client
from mcp.types import CallToolResult, TextContent
from psycopg_pool import AsyncConnectionPool

from crm_server.repo import CrmRepo
from crm_server.seed import INTERNAL_MARKER as CRM_MARKER
from crm_server.seed import Dataset as CrmDataset
from crm_server.tools import INSTRUCTIONS as CRM_INSTRUCTIONS
from crm_server.tools import build_toolset as build_crm_toolset
from handbook_server.embedding import Embedder
from handbook_server.repo import HandbookRepo
from handbook_server.tools import INSTRUCTIONS as HANDBOOK_INSTRUCTIONS
from handbook_server.tools import build_toolset as build_handbook_toolset
from harborline_setup.handbook_seed import Dataset as HandbookDataset
from ticketing_server.repo import TicketRepo
from ticketing_server.seed import INTERNAL_MARKER as TICKETING_MARKER
from ticketing_server.seed import Dataset as TicketingDataset
from ticketing_server.tools import INSTRUCTIONS as TICKETING_INSTRUCTIONS
from ticketing_server.tools import build_toolset as build_ticketing_toolset

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

RESTRICTED_IDS = ["DOC-023", "DOC-024", "DOC-030"]
CODE_PHRASES = ["HERON-LANTERN-7", "PELICAN-VESPER-31", "KESTREL-TALLOW-58"]


def _everything(result: CallToolResult) -> str:
    """A result as a model or a client would see it: the structured data and the text."""
    text = "\n".join(b.text for b in result.content if isinstance(b, TextContent))
    return f"{json.dumps(result.structured_content)}\n{text}"


def _assert_no_leak(outputs: list[str], values: list[str]) -> None:
    everything = "\n".join(outputs)
    assert len(values) > 20, "the seed should hold plenty of internal text to leak"
    assert [value for value in values if value in everything] == []


@pytest.fixture
async def ticketing_client(ticketing_pool: AsyncConnectionPool) -> AsyncIterator[Client]:
    server = build_ticketing_toolset(TicketRepo(ticketing_pool)).build_server(
        "ticketing", "0", TICKETING_INSTRUCTIONS
    )
    async with Client(server, mode="legacy") as client:
        yield client


@pytest.fixture
async def crm_client(crm_pool: AsyncConnectionPool) -> AsyncIterator[Client]:
    server = build_crm_toolset(CrmRepo(crm_pool)).build_server("crm", "0", CRM_INSTRUCTIONS)
    async with Client(server, mode="legacy") as client:
        yield client


@pytest.fixture
async def handbook_client(
    handbook_pool: AsyncConnectionPool, embedder: Embedder
) -> AsyncIterator[Client]:
    server = build_handbook_toolset(HandbookRepo(handbook_pool, embedder)).build_server(
        "handbook", "0", HANDBOOK_INSTRUCTIONS
    )
    async with Client(server, mode="legacy") as client:
        yield client


async def test_no_ticketing_read_tool_returns_an_internal_value(
    ticketing_client: Client, ticketing_data: TicketingDataset
) -> None:
    outputs = []
    for ticket in ticketing_data.tickets:
        outputs.append(
            _everything(await ticketing_client.call_tool("get_ticket", {"ticket_id": ticket.id}))
        )
    for number in range(1, 41):
        arguments = {"account_id": f"ACC-{number:05d}", "limit": 20}
        outputs.append(_everything(await ticketing_client.call_tool("list_tickets", arguments)))

    _assert_no_leak(outputs, ticketing_data.internal_values())
    assert TICKETING_MARKER not in "\n".join(outputs)


async def test_no_crm_read_tool_returns_an_internal_value(
    crm_client: Client, crm_data: CrmDataset
) -> None:
    outputs = []
    for account in crm_data.accounts:
        outputs.append(
            _everything(await crm_client.call_tool("get_account", {"account_id": account.id}))
        )
        arguments = {"account_id": account.id, "limit": 20}
        outputs.append(_everything(await crm_client.call_tool("list_deals", arguments)))
    for offset in range(0, 60, 20):
        outputs.append(
            _everything(await crm_client.call_tool("list_deals", {"limit": 20, "offset": offset}))
        )
    words = {
        word.lower()
        for account in crm_data.accounts
        for word in re.findall(r"[A-Za-z]{2,}", f"{account.name} {account.industry}")
    }
    for word in sorted(words):
        arguments = {"query": word, "limit": 20}
        outputs.append(_everything(await crm_client.call_tool("search_accounts", arguments)))
    for tier in ("bronze", "silver", "gold"):
        arguments = {"query": "er", "tier": tier, "limit": 20}
        outputs.append(_everything(await crm_client.call_tool("search_accounts", arguments)))

    _assert_no_leak(outputs, crm_data.internal_values())
    assert CRM_MARKER not in "\n".join(outputs)


async def test_no_handbook_read_tool_returns_restricted_text(
    handbook_client: Client, handbook_data: HandbookDataset
) -> None:
    outputs = []
    queries = [
        *[d.title for d in handbook_data.documents],
        *{c.heading for c in handbook_data.chunks},
        *[c.text[:150] for c in handbook_data.chunks if c.document_id in RESTRICTED_IDS],
        *CODE_PHRASES,
    ]
    for query in queries:
        for category in (None, "security", "expenses"):
            arguments: dict[str, object] = {"query": query[:200], "limit": 10}
            if category:
                arguments["category"] = category
            outputs.append(_everything(await handbook_client.call_tool("search", arguments)))
    for number in range(1, 31):
        arguments = {"document_id": f"DOC-{number:03d}"}
        outputs.append(_everything(await handbook_client.call_tool("get_document", arguments)))

    _assert_no_leak(outputs, handbook_data.restricted_texts())
    assert [phrase for phrase in CODE_PHRASES if phrase in "\n".join(outputs)] == []
    assert len(handbook_data.restricted_texts()) >= 3 * 5  # body, title and chunks of each


def test_the_scan_itself_notices_a_planted_value() -> None:
    values = [f"internal value {n}" for n in range(25)]

    with pytest.raises(AssertionError):
        _assert_no_leak(["a harmless output", "oops: internal value 7 leaked"], values)
    _assert_no_leak(["a harmless output"], values)
