"""The CRM tools, against the seeded fictional database as the crm_app role."""

import json
import logging
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from mcp.client import Client
from mcp.shared.exceptions import MCPError
from mcp.types import INVALID_PARAMS, CallToolResult
from psycopg_pool import AsyncConnectionPool

from crm_server.models import MAX_NOTES_RETURNED, REGIONS
from crm_server.repo import CrmRepo, like_pattern
from crm_server.seed import (
    ACCOUNT_COUNT,
    CONTACT_COUNT,
    DEAL_COUNT,
    INTERNAL_MARKER,
    NOTE_COUNT,
    Dataset,
    build_dataset,
    load_extra_records,
)
from crm_server.tools import INSTRUCTIONS, build_toolset
from mcp_common.notice import FICTIONAL_NOTICE
from mcp_common.schema_contract import check_input_schema

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

TOOLS = {"search_accounts", "get_account", "list_deals"}
INTERNAL_FIELDS = {
    "credit_limit_internal",
    "risk_rating_internal",
    "internal_notes",
    "floor_price_cents",
}


@pytest.fixture
async def client(crm_pool: AsyncConnectionPool) -> AsyncIterator[Client]:
    server = build_toolset(CrmRepo(crm_pool)).build_server("crm", "0", INSTRUCTIONS)
    async with Client(server, mode="legacy") as connected:
        yield connected


def _structured(result: CallToolResult) -> dict[str, Any]:
    assert not result.is_error, result.content
    assert isinstance(result.structured_content, dict)
    return result.structured_content


# --- the tool surface ------------------------------------------------------------------


async def test_the_server_offers_three_read_only_tools(client: Client) -> None:
    tools = (await client.list_tools()).tools

    assert {tool.name for tool in tools} == TOOLS
    assert all(tool.annotations and tool.annotations.read_only_hint for tool in tools)


async def test_every_input_schema_meets_the_contract(client: Client) -> None:
    for tool in (await client.list_tools()).tools:
        assert check_input_schema(tool.input_schema) == [], tool.name


def test_the_instructions_say_the_data_is_fictional() -> None:
    assert "fictional" in INSTRUCTIONS


async def test_every_result_carries_the_fictional_notice(client: Client) -> None:
    results = [
        await client.call_tool("search_accounts", {"query": "marina"}),
        await client.call_tool("get_account", {"account_id": "ACC-00001"}),
        await client.call_tool("list_deals", {}),
    ]

    assert [_structured(result)["notice"] for result in results] == [FICTIONAL_NOTICE] * 3


# --- search_accounts ---------------------------------------------------------------------


async def test_search_finds_an_account_by_a_word_in_its_name(
    client: Client, crm_data: Dataset
) -> None:
    target = crm_data.accounts[3]
    word = target.name.split()[0]

    found = _structured(await client.call_tool("search_accounts", {"query": word.lower()}))

    ids = [account["id"] for account in found["accounts"]]
    assert target.id in ids
    summary = next(a for a in found["accounts"] if a["id"] == target.id)
    assert set(summary) == {"id", "name", "industry", "region", "tier", "account_manager"}


async def test_search_ranks_a_name_match_above_a_description_match(
    client: Client, crm_data: Dataset
) -> None:
    found = _structured(
        await client.call_tool("search_accounts", {"query": "marina", "limit": 20})
    )["accounts"]

    in_name = [a for a in found if "marina" in a["name"].lower()]
    assert in_name
    assert [a["id"] for a in found[: len(in_name)]] == [a["id"] for a in in_name]


def _matches(account: Any, query: str) -> bool:
    return query in f"{account.name} {account.industry} {account.about}".lower()


async def test_search_filters_by_tier_and_region(client: Client, crm_data: Dataset) -> None:
    region = crm_data.accounts[0].region
    gold = _structured(
        await client.call_tool("search_accounts", {"query": "er", "tier": "gold", "limit": 20})
    )["accounts"]
    regional = _structured(
        await client.call_tool("search_accounts", {"query": "er", "region": region, "limit": 20})
    )["accounts"]

    assert {a["tier"] for a in gold} == {"gold"}
    assert {a["region"] for a in regional} == {region}
    assert {a["id"] for a in regional} == {
        a.id for a in crm_data.accounts if a.region == region and _matches(a, "er")
    }


async def test_search_treats_like_wildcards_as_plain_text(client: Client) -> None:
    for query in ("%%", "__", "\\\\", "a%"):
        found = _structured(await client.call_tool("search_accounts", {"query": query}))
        assert found["accounts"] == [], query


def test_like_pattern_escapes_the_wildcards() -> None:
    assert like_pattern(r"50%_\x") == r"%50\%\_\\x%"


async def test_search_offset_and_limit_page_through_the_results(
    client: Client, crm_data: Dataset
) -> None:
    both = _structured(await client.call_tool("search_accounts", {"query": "er", "limit": 20}))
    first = _structured(await client.call_tool("search_accounts", {"query": "er", "limit": 5}))
    second = _structured(
        await client.call_tool("search_accounts", {"query": "er", "limit": 5, "offset": 5})
    )
    default = _structured(await client.call_tool("search_accounts", {"query": "er"}))

    ids = [a["id"] for a in both["accounts"]]
    assert len(ids) == 20
    assert [a["id"] for a in first["accounts"] + second["accounts"]] == ids[:10]
    assert len(default["accounts"]) == 10


@pytest.mark.parametrize(
    "arguments",
    [
        {"query": "a"},
        {"query": "x" * 101},
        {"query": "marina", "limit": 0},
        {"query": "marina", "limit": 21},
        {"query": "marina", "offset": -1},
        {"query": "marina", "offset": 10001},
        {"query": "marina", "tier": "platinum"},
        {"query": "marina", "region": "moon"},
        {"query": "marina", "limit": "5"},
    ],
)
async def test_search_refuses_out_of_bounds_values(
    client: Client, arguments: dict[str, Any]
) -> None:
    with pytest.raises(MCPError) as error:
        await client.call_tool("search_accounts", arguments)

    assert error.value.code == INVALID_PARAMS


# --- get_account -------------------------------------------------------------------------


async def test_get_account_returns_the_public_fields_contacts_and_the_newest_notes(
    client: Client, crm_data: Dataset
) -> None:
    account = max(
        crm_data.accounts,
        key=lambda a: sum(1 for n in crm_data.notes if n.account_id == a.id),
    )
    notes = sorted(
        (n for n in crm_data.notes if n.account_id == account.id),
        key=lambda n: n.occurred_at,
        reverse=True,
    )
    assert len(notes) > MAX_NOTES_RETURNED

    detail = _structured(await client.call_tool("get_account", {"account_id": account.id}))

    assert detail["name"] == account.name
    assert detail["about"] == account.about
    assert detail["notes_total"] == len(notes)
    assert len(detail["notes"]) == MAX_NOTES_RETURNED
    assert [n["occurred_at"] for n in detail["notes"]] == sorted(
        (n["occurred_at"] for n in detail["notes"]), reverse=True
    )
    contacts = [c for c in crm_data.contacts if c.account_id == account.id]
    assert [c["id"] for c in detail["contacts"]] == [c.id for c in contacts]
    assert INTERNAL_FIELDS.isdisjoint(detail)


async def test_get_account_for_an_unknown_id_is_a_tool_error(client: Client) -> None:
    result = await client.call_tool("get_account", {"account_id": "ACC-99999"})

    assert result.is_error


@pytest.mark.parametrize(
    "query", ["mar\x00ina", "marina\n", "ma\trina", "\x7fmarina", "ma\x85rina"]
)
async def test_search_refuses_a_control_character_and_logs_no_error(
    client: Client, query: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    with pytest.raises(MCPError) as error:
        await client.call_tool("search_accounts", {"query": query})

    assert error.value.code == INVALID_PARAMS
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []


async def test_search_still_accepts_ordinary_spaces(client: Client) -> None:
    found = _structured(await client.call_tool("search_accounts", {"query": "marina  harbor"}))

    assert "accounts" in found


@pytest.mark.parametrize("account_id", ["ACC-1", "acc-00001", "ACC-00001 OR 1=1", ""])
async def test_get_account_refuses_a_malformed_id(client: Client, account_id: str) -> None:
    with pytest.raises(MCPError) as error:
        await client.call_tool("get_account", {"account_id": account_id})

    assert error.value.code == INVALID_PARAMS


# --- list_deals --------------------------------------------------------------------------


async def test_list_deals_filters_and_never_returns_a_floor_price(
    client: Client, crm_data: Dataset
) -> None:
    account = crm_data.deals[0].account_id
    expected = {d.id for d in crm_data.deals if d.account_id == account}

    by_account = _structured(
        await client.call_tool("list_deals", {"account_id": account, "limit": 20})
    )["deals"]
    won = _structured(await client.call_tool("list_deals", {"stage": "won", "limit": 20}))["deals"]

    assert {d["id"] for d in by_account} == expected
    assert {d["stage"] for d in won} == {"won"}
    for deal in by_account + won:
        assert set(deal) == {
            "id", "account_id", "name", "stage", "amount_cents", "close_date", "owner",
        }  # fmt: skip
        assert isinstance(deal["amount_cents"], int)


async def test_list_deals_orders_by_close_date_newest_first_and_pages(client: Client) -> None:
    both = _structured(await client.call_tool("list_deals", {"limit": 20}))["deals"]
    second = _structured(await client.call_tool("list_deals", {"limit": 5, "offset": 5}))["deals"]

    assert [d["close_date"] for d in both] == sorted((d["close_date"] for d in both), reverse=True)
    assert [d["id"] for d in second] == [d["id"] for d in both[5:10]]


@pytest.mark.parametrize(
    "arguments",
    [{"limit": 0}, {"limit": 21}, {"offset": 10001}, {"stage": "pending"}, {"account_id": "ACC-1"}],
)
async def test_list_deals_refuses_out_of_bounds_values(
    client: Client, arguments: dict[str, Any]
) -> None:
    with pytest.raises(MCPError) as error:
        await client.call_tool("list_deals", arguments)

    assert error.value.code == INVALID_PARAMS


# --- strict inputs ---------------------------------------------------------------------------

VALID_ARGUMENTS: dict[str, dict[str, Any]] = {
    "search_accounts": {"query": "marina"},
    "get_account": {"account_id": "ACC-00001"},
    "list_deals": {},
}


@pytest.mark.parametrize("tool", sorted(VALID_ARGUMENTS))
async def test_an_unknown_argument_is_refused_before_anything_runs(
    client: Client, tool: str
) -> None:
    with pytest.raises(MCPError) as error:
        await client.call_tool(tool, {**VALID_ARGUMENTS[tool], "credit_limit_internal": 1})

    assert error.value.code == INVALID_PARAMS


# --- no internal value ever leaves -------------------------------------------------------------


async def test_the_database_role_cannot_read_the_internal_columns(crm_app_url: str) -> None:
    async with await psycopg.AsyncConnection.connect(crm_app_url, autocommit=True) as connection:
        for query in (
            b"SELECT credit_limit_internal FROM crm.accounts",
            b"SELECT risk_rating_internal FROM crm.accounts",
            b"SELECT internal_notes FROM crm.accounts",
            b"SELECT floor_price_cents FROM crm.deals",
            b"SELECT * FROM crm.accounts",
            b"SELECT * FROM crm.deals",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                await connection.execute(query)


# --- the seed --------------------------------------------------------------------------------


def test_the_seed_is_deterministic() -> None:
    assert build_dataset() == build_dataset()
    assert build_dataset(seed=1) != build_dataset()


def test_the_seed_has_the_documented_shape() -> None:
    dataset = build_dataset()

    assert (
        len(dataset.accounts), len(dataset.contacts), len(dataset.deals), len(dataset.notes)
    ) == (ACCOUNT_COUNT, CONTACT_COUNT, DEAL_COUNT, NOTE_COUNT)  # fmt: skip
    assert {a.id for a in dataset.accounts} == {f"ACC-{n:05d}" for n in range(1, 41)}
    assert {a.region for a in dataset.accounts} <= set(REGIONS)
    assert {a.tier for a in dataset.accounts} == {"bronze", "silver", "gold"}
    assert {d.stage for d in dataset.deals} == {
        "prospecting", "proposal", "negotiation", "won", "lost",
    }  # fmt: skip
    assert all(re.fullmatch(r"[a-z]+\.[a-z]+", a.account_manager) for a in dataset.accounts)


def test_every_internal_text_value_starts_with_the_marker_and_numbers_are_distinctive() -> None:
    dataset = build_dataset()
    text_values = [v for a in dataset.accounts for v in (a.internal_notes, a.risk_rating_internal)]
    public_amounts = {str(d.amount_cents) for d in dataset.deals}

    assert all(value.startswith(INTERNAL_MARKER) for value in text_values)
    assert all(str(a.credit_limit_internal).endswith("7777") for a in dataset.accounts)
    assert all(str(d.floor_price_cents).endswith("4242") for d in dataset.deals)
    assert not any(value in public_amounts for value in dataset.internal_values())
    assert len(dataset.internal_values()) == 3 * ACCOUNT_COUNT + DEAL_COUNT


def test_the_seed_contains_no_real_looking_contact_data() -> None:
    dataset = build_dataset()
    text = json.dumps(
        [(c.email, c.phone) for c in dataset.contacts] + [n.body for n in dataset.notes]
    )

    assert all(domain.endswith(".example") for domain in re.findall(r"@([\w.-]+)", text))
    assert all(phone.startswith("555-01") for phone in re.findall(r"\b\d{3}-\d{4}\b", text))


async def test_the_seed_in_the_database_is_the_seed_in_code(
    crm_app_url: str, crm_data: Dataset
) -> None:
    async with await psycopg.AsyncConnection.connect(crm_app_url) as connection:
        await connection.execute("SET search_path TO crm")
        cursor = await connection.execute("SELECT id, name FROM accounts ORDER BY id")
        rows = await cursor.fetchall()

    assert rows == [(a.id, a.name) for a in crm_data.accounts]


def test_extra_records_are_merged_into_the_seed(tmp_path: Path) -> None:
    extra = tmp_path / "extra.json"
    extra.write_text(
        json.dumps(
            {
                "accounts": [
                    {
                        "name": "Planted Co",
                        "industry": "x",
                        "region": "northeast",
                        "about": "planted text",
                        "account_manager": "a.b",
                    }
                ],
                "notes": [{"account_id": "ACC-00001", "author": "x.y", "body": "planted"}],
            }
        )
    )
    dataset = build_dataset()

    load_extra_records(dataset, extra)

    assert dataset.accounts[-1].id == f"ACC-{ACCOUNT_COUNT + 1:05d}"
    assert dataset.accounts[-1].internal_notes.startswith(INTERNAL_MARKER)
    assert dataset.notes[-1].body == "planted"


def test_extra_records_reject_unknown_fields(tmp_path: Path) -> None:
    extra = tmp_path / "extra.json"
    extra.write_text(json.dumps({"accounts": [], "surprise": 1}))

    with pytest.raises(ValueError, match="surprise"):
        load_extra_records(build_dataset(), extra)
