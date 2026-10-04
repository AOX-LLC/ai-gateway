"""The writer against a real database: what it stores, and what it survives."""

import time
from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import anyio
import psycopg
import pytest
from psycopg import AsyncConnection

from ai_gateway.seams.events import GatewayEvent
from ai_gateway.telemetry import writer as writer_module
from ai_gateway.telemetry.buffer import Row, TelemetryBuffer
from ai_gateway.telemetry.rows import pipeline_config_row, rows_for_event
from ai_gateway.telemetry.writer import TelemetryWriter
from tests.test_upstreams import eventually

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
SHA = "e" * 64


def _call_rows(n: int = 1) -> list[Row]:
    rows: list[Row] = []
    for _ in range(n):
        event = GatewayEvent(
            action="gateway.tool_call",
            actor_id=f"client:{uuid4()}",
            subject_id="tickets__get_ticket",
            payload={
                "request_id": str(uuid4()),
                "client_name": "harborline-support-bot",
                "layers": [
                    {"layer": "scope", "hook": "before_call", "mode": "enforce", "verdict": "allow"}
                ],
                "duration_ms": 1.0,
                "outcome": "forwarded",
                "namespace": "tickets",
            },
            occurred_at=NOW,
        )
        rows += rows_for_event(event)
    return rows


async def _count(url: str, table: str) -> int:
    async with await AsyncConnection.connect(url) as connection:
        cursor = await connection.execute(f"SELECT count(*) FROM telemetry.{table}".encode())
        row = await cursor.fetchone()
    assert row is not None
    return int(row[0])


@pytest.fixture
def buffer() -> TelemetryBuffer:
    return TelemetryBuffer(10_000)


@pytest.fixture
def writer(buffer: TelemetryBuffer, writer_url: str) -> TelemetryWriter:
    return TelemetryWriter(buffer, writer_url, flush_interval_s=0.02)


async def test_rows_of_every_table_are_written_in_one_flush(
    telemetry: None, buffer: TelemetryBuffer, writer: TelemetryWriter, test_database_url: str
) -> None:
    buffer.put(*_call_rows(3))
    buffer.put(pipeline_config_row(SHA, [{"name": "scope", "mode": "enforce"}], NOW))

    await writer.close()

    assert await _count(test_database_url, "requests") == 3
    assert await _count(test_database_url, "layer_verdicts") == 3
    assert await _count(test_database_url, "pipeline_configs") == 1
    assert writer.status().status == "ok"
    assert writer.status().written_total == 7
    assert writer.status().queue_depth == 0


async def test_writing_the_same_rows_again_stores_them_once(
    telemetry: None, buffer: TelemetryBuffer, writer: TelemetryWriter, test_database_url: str
) -> None:
    rows = _call_rows(2)

    buffer.put(*rows)
    await writer.close()
    buffer.put(*rows)
    await writer.close()

    assert await _count(test_database_url, "requests") == 2
    assert await _count(test_database_url, "layer_verdicts") == 2


async def test_a_row_the_database_refuses_is_dropped_and_the_rest_are_written(
    telemetry: None, buffer: TelemetryBuffer, writer: TelemetryWriter, test_database_url: str
) -> None:
    first, poison, last = _call_rows(1), _call_rows(1), _call_rows(1)
    poison[0].values["tool"] = "t" * 65  # violates the column's CHECK
    buffer.put(*first, *poison, *last)

    await writer.close()

    status = writer.status()
    assert (status.status, status.queue_depth, status.rejected_total) == ("ok", 0, 1)
    assert await _count(test_database_url, "requests") == 2
    # 2 requests and 3 layer verdicts were written; the refused request row was not.
    assert status.written_total == len(first) + len(last) + len(poison) - 1


async def test_a_database_failure_while_splitting_a_refused_batch_loses_nothing(
    telemetry: None,
    buffer: TelemetryBuffer,
    writer_url: str,
    test_database_url: str,
    flaky: "_FlakyConnect",
) -> None:
    """The batch is refused, then the connection fails during the one-by-one pass: the rows
    must stay queued and the writer must say it is degraded, not write them off."""
    flaky.mode = "pass"
    writer = TelemetryWriter(buffer, writer_url, flush_interval_s=0.02)
    first, poison, last = _call_rows(1), _call_rows(1), _call_rows(1)
    poison[0].values["tool"] = "t" * 65
    buffer.put(*first, *poison, *last)
    real_insert = writer._insert
    calls = {"n": 0}

    async def failing_after_the_refusal(rows: list[Row]) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            await real_insert(rows)  # the whole batch: refused by the CHECK
        else:
            raise psycopg.OperationalError("connection lost")

    writer._insert = failing_after_the_refusal  # type: ignore[method-assign]

    await writer._write_next_batch()

    status = writer.status()
    assert status.status == "degraded"
    assert status.written_total == 0
    assert status.queue_depth == len(first) + len(poison) + len(last)
    assert await _count(test_database_url, "requests") == 0


class _FlakyConnect:
    """Stands in for AsyncConnection.connect: refuses, or never answers, until told not to."""

    def __init__(self, real: Callable[..., Any]) -> None:
        self.real = real
        self.mode = "refuse"
        self.attempts = 0

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.attempts += 1
        if self.mode == "refuse":
            raise psycopg.OperationalError("connection refused")
        if self.mode == "hang":
            await anyio.sleep_forever()
        return await self.real(*args, **kwargs)


@pytest.fixture
def flaky(monkeypatch: pytest.MonkeyPatch) -> _FlakyConnect:
    connect = _FlakyConnect(AsyncConnection.connect)
    # Only the writer's own reference: the test's checks must still reach the database.
    monkeypatch.setattr(writer_module, "AsyncConnection", SimpleNamespace(connect=connect))
    monkeypatch.setattr(writer_module, "_BACKOFF_START_S", 0.05)
    return connect


@pytest.mark.parametrize("mode", ["refuse", "hang"])
async def test_a_database_that_is_down_or_silent_costs_the_request_path_nothing(
    telemetry: None, writer_url: str, test_database_url: str, flaky: _FlakyConnect, mode: str
) -> None:
    flaky.mode = mode
    buffer = TelemetryBuffer(60)
    writer = TelemetryWriter(buffer, writer_url, flush_interval_s=0.02, connect_timeout_s=0.2)

    async with anyio.create_task_group() as tasks:
        await tasks.start(writer.run)
        started = time.perf_counter()
        for _ in range(100):  # 200 rows, far more than the buffer holds
            buffer.put(*_call_rows())
        put_seconds = time.perf_counter() - started
        await eventually(lambda: writer.status().status == "degraded", timeout_s=10)

        assert put_seconds < 0.5, "queueing must not wait for the database"
        assert writer.status().dropped_total > 0
        assert writer.status().queue_depth <= 60 + 200  # the buffer's room and one batch
        assert await _count(test_database_url, "requests") == 0

        flaky.mode = "pass"  # the database comes back
        await eventually(
            lambda: writer.status().queue_depth == 0 and writer.status().status == "ok",
            timeout_s=10,
        )
        tasks.cancel_scope.cancel()

    assert await _count(test_database_url, "requests") > 0
    assert flaky.attempts >= 2


async def test_cancelling_the_writer_flushes_what_is_left(
    telemetry: None, buffer: TelemetryBuffer, writer_url: str, test_database_url: str
) -> None:
    writer = TelemetryWriter(buffer, writer_url, flush_interval_s=30.0)  # it would wait 30 s
    async with anyio.create_task_group() as tasks:
        await tasks.start(writer.run)
        await anyio.sleep(0.05)
        buffer.put(*_call_rows(4))
        tasks.cancel_scope.cancel()

    assert await _count(test_database_url, "requests") == 4


async def test_an_unclassified_classifier_verdict_is_stored(
    telemetry: None, buffer: TelemetryBuffer, writer: TelemetryWriter, test_database_url: str
) -> None:
    rows = _call_rows(1)
    layers = {
        "layer": "classifier",
        "hook": "after_call",
        "mode": "enforce",
        "verdict": "unclassified",
        "code": "classifier_unrecorded",
        "score": 2,
    }
    event = GatewayEvent(
        action="gateway.tool_call",
        actor_id=f"client:{uuid4()}",
        subject_id="tickets__get_ticket",
        payload={
            "request_id": str(uuid4()),
            "client_name": "harborline-support-bot",
            "layers": [layers],
            "duration_ms": 1.0,
            "outcome": "forwarded",
            "namespace": "tickets",
        },
        occurred_at=NOW,
    )
    buffer.put(*rows, *rows_for_event(event))

    await writer.close()

    assert writer.status().rejected_total == 0
    assert await _count(test_database_url, "layer_verdicts") == 2
