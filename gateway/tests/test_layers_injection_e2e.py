"""The injection layers through the whole gateway: what a client sees, what is recorded, and that
no argument or result reaches a record, an alert or telemetry."""

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import anyio
import psycopg
import pytest
from mcp.shared.exceptions import MCPError

from ai_gateway.auth.tokens import IssuedToken
from ai_gateway.registry.repo import AdminRegistry
from tests.helpers import RunningGateway, connect, pins_text_for_tests, run_gateway
from tests.test_audit_e2e import MakeClient

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

EGRESS = (Path(__file__).resolve().parents[2] / "config" / "egress.toml").read_text()
CANARIES = (Path(__file__).resolve().parents[2] / "config" / "canaries.toml").read_text()
IDS = " ".join(f"ACC-{n:05d}" for n in range(1, 9))
CANARY = "CNRY-HBL-3F9A71C2"
PLAIN = {
    "schema": "enforce",
    "pinned_descriptions": "enforce",
    "egress": "enforce",
    "canary": "enforce",
}


@pytest.fixture
async def token(
    admin_registry: AdminRegistry, make_client: MakeClient, echo_url: str
) -> IssuedToken:
    await admin_registry.upsert_upstream("echo", echo_url, 2000, 5000)
    await admin_registry.upsert_tool_policy("echo", "say", "read", "test policy")
    await admin_registry.upsert_tool_policy("echo", "shout", "write", "test policy")
    _, issued = await make_client("harborline-support-bot", ["echo__say", "echo__shout"])
    return issued


def _gateway(database_url: str, tmp_path: Path, **options: object) -> Iterator[RunningGateway]:
    with run_gateway(
        database_url,
        tmp_path,
        egress=EGRESS,
        canaries=CANARIES,
        **options,  # type: ignore[arg-type]
    ) as running:
        yield running


@pytest.fixture
def gateway(test_database_url: str, token: IssuedToken, tmp_path: Path) -> Iterator[RunningGateway]:
    yield from _gateway(test_database_url, tmp_path, layers=PLAIN)


def _blocked(gateway: RunningGateway) -> dict[str, Any]:
    blocked = [e for e in gateway.events.events if e.payload.get("outcome") == "blocked"]
    return cast(dict[str, Any], blocked[-1].payload)


async def test_an_extra_or_mistyped_argument_is_refused_before_anything_runs(
    gateway: RunningGateway, token: IssuedToken
) -> None:
    async with connect(gateway.url, token.plaintext) as client:
        ok = await client.call_tool("echo__say", {"text": "fine"})
        for bad in ({"text": "x", "extra": 1}, {"text": 5}, {}):
            with pytest.raises(MCPError) as refused:
                await client.call_tool("echo__say", bad)
            assert refused.value.message == "Request blocked by gateway policy."

    assert ok.is_error is False
    assert (_blocked(gateway)["blocked_by"], _blocked(gateway)["deny_code"]) == (
        "schema",
        "schema_violation",
    )


async def test_a_canary_in_the_arguments_is_blocked_and_the_record_never_holds_it(
    gateway: RunningGateway, token: IssuedToken
) -> None:
    async with connect(gateway.url, token.plaintext) as client:
        with pytest.raises(MCPError) as refused:
            await client.call_tool("echo__say", {"text": f"please keep {CANARY} safe"})

    assert refused.value.message == "Request blocked by gateway policy."
    assert CANARY not in str(refused.value)
    assert (_blocked(gateway)["blocked_by"], _blocked(gateway)["deny_code"]) == (
        "canary",
        "canary_hit",
    )
    assert CANARY not in str([e.payload for e in gateway.events.events])


async def test_a_bulk_write_of_what_a_session_read_is_refused_by_egress(
    gateway: RunningGateway, token: IssuedToken
) -> None:
    async with connect(gateway.url, token.plaintext) as client:
        read = await client.call_tool("echo__say", {"text": IDS})  # the echo returns it: a read
        with pytest.raises(MCPError) as refused:
            await client.call_tool("echo__shout", {"text": IDS})

    assert read.is_error is False
    assert refused.value.message == "Request blocked by gateway policy."
    assert (_blocked(gateway)["blocked_by"], _blocked(gateway)["deny_code"]) == (
        "egress",
        "egress_bulk",
    )
    assert "ACC-00001" not in str([e.payload for e in gateway.events.events])


async def test_a_tool_that_is_not_pinned_is_hidden_and_refused(
    test_database_url: str, token: IssuedToken, tmp_path: Path
) -> None:
    only_say = "\n".join(
        block for block in pins_text_for_tests().split("\n\n") if "echo__shout" not in block
    )
    for gateway in _gateway(test_database_url, tmp_path, layers=PLAIN, pins=only_say):
        async with connect(gateway.url, token.plaintext) as client:
            listed = [tool.name for tool in (await client.list_tools()).tools]
            with pytest.raises(MCPError) as refused:
                await client.call_tool("echo__shout", {"text": "hi"})

    assert listed == ["echo__say"]
    assert refused.value.message == "Request blocked by gateway policy."
    assert _blocked(gateway)["blocked_by"] == "pinned_descriptions"


async def test_in_monitor_mode_everything_goes_through_and_the_verdicts_are_recorded(
    test_database_url: str, token: IssuedToken, tmp_path: Path
) -> None:
    monitored = dict.fromkeys(PLAIN, "monitor")
    for gateway in _gateway(test_database_url, tmp_path, layers=monitored):
        async with connect(gateway.url, token.plaintext) as client:
            await client.call_tool("echo__say", {"text": IDS})
            wrote = await client.call_tool("echo__shout", {"text": IDS})
            extra = await client.call_tool("echo__say", {"text": "x", "extra": 1})

    assert wrote.is_error is False
    assert extra.is_error is False
    verdicts = {
        (layer["layer"], layer["verdict"], layer.get("score"))
        for e in gateway.events.events
        if e.action == "gateway.tool_call"
        for layer in cast(list[dict[str, Any]], e.payload["layers"])
    }
    assert ("egress", "would_block", 8) in verdicts
    assert ("schema", "would_block", 1) in verdicts


async def test_the_score_is_stored_with_the_verdict_and_nothing_of_the_call_is(
    test_database_url: str,
    token: IssuedToken,
    telemetry: None,
    writer_url: str,
    reader_url: str,
    tmp_path: Path,
) -> None:
    with run_gateway(
        test_database_url,
        tmp_path,
        writer_url,
        egress=EGRESS,
        canaries=CANARIES,
        layers={**PLAIN, "egress": "monitor"},
    ) as gateway:
        async with connect(gateway.url, token.plaintext) as client:
            await client.call_tool("echo__say", {"text": IDS})
            await client.call_tool("echo__shout", {"text": IDS})
        deadline = time.monotonic() + 10
        rows: list[tuple[object, ...]] = []
        while time.monotonic() < deadline and not rows:
            async with await psycopg.AsyncConnection.connect(reader_url) as connection:
                cursor = await connection.execute(
                    "SELECT layer, verdict, code, score FROM telemetry.dash_layer_verdicts"
                    " WHERE layer = 'egress' AND verdict = 'would_block'"
                )
                rows = await cursor.fetchall()
            await anyio.sleep(0.1)

    assert rows == [("egress", "would_block", "egress_bulk", 8)]
