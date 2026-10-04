"""Write buffered telemetry rows to Postgres, in the background.

The writer is the only thing that talks to the database, and nothing waits for it. It takes
batches from the buffer and inserts each in one transaction. A failure never reaches a
request:

- A database that is down or slow (connect, statement and total time are all bounded) keeps
  the batch in the writer and retries with backoff from 1 s to 30 s, while the buffer goes on
  accepting rows and drops the oldest when it is full.
- A row the database refuses (a CHECK, say) cannot ever succeed, so it is not retried: the
  batch is split, the refused rows are dropped and counted, and the rest are written.
"""

import logging
import math
import time
from collections import defaultdict
from contextlib import suppress
from dataclasses import dataclass

import anyio
import psycopg
from psycopg import AsyncConnection, errors, sql

from ai_gateway.telemetry import SCHEMA, TABLES
from ai_gateway.telemetry.buffer import Row, TelemetryBuffer

logger = logging.getLogger(__name__)

COLUMNS: dict[str, tuple[str, ...]] = {
    "requests": (
        "request_id", "ts", "kind", "client_id", "client_name", "tool", "namespace", "effect",
        "effect_source", "outcome", "blocked_by", "deny_code", "upstream_status", "duration_ms",
        "upstream_duration_ms", "args_sha256", "protocol_version", "pipeline_config_sha256",
        "trace_id", "tools_available", "tools_returned",
    ),
    "layer_verdicts": (
        "request_id", "ordinal", "ts", "layer", "hook", "mode", "verdict", "code",
        "tools_removed", "duration_ms", "score",
    ),
    "auth_failures": ("event_id", "ts", "reason", "lookup_id"),
    "spans": (
        "trace_id", "span_id", "parent_span_id", "ts", "name", "duration_us", "status",
        "request_id", "attrs",
    ),
    "pipeline_configs": ("sha256", "first_seen", "layers"),
    "model_usage": (
        "usage_id", "request_id", "ts", "layer", "purpose", "model", "tier", "mode",
        "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "cost_usd",
        "latency_ms", "status",
    ),
}  # fmt: skip
"""The columns written, per table. A row's other keys are ignored."""

_BACKOFF_START_S = 1.0
_BACKOFF_MAX_S = 30.0
_LOG_EVERY_S = 60.0


@dataclass(frozen=True)
class WriterStatus:
    status: str
    """`ok`, or `degraded` while the last write failed."""
    queue_depth: int
    dropped_total: int
    rejected_total: int
    written_total: int


class TelemetryWriter:
    def __init__(
        self,
        buffer: TelemetryBuffer,
        database_url: str,
        *,
        batch_size: int = 200,
        flush_interval_s: float = 1.0,
        connect_timeout_s: float = 2.0,
        write_timeout_s: float = 5.0,
    ) -> None:
        self._buffer = buffer
        self._url = database_url
        self._batch_size = batch_size
        self._flush_interval_s = flush_interval_s
        self._connect_timeout_s = connect_timeout_s
        self._write_timeout_s = write_timeout_s
        self._connection: AsyncConnection | None = None
        self._pending: list[Row] = []
        self._failing = False
        self._last_logged = 0.0
        self._rejected_total = 0
        self._written_total = 0

    def status(self) -> WriterStatus:
        return WriterStatus(
            status="degraded" if self._failing else "ok",
            queue_depth=len(self._buffer) + len(self._pending),
            dropped_total=self._buffer.dropped_total,
            rejected_total=self._rejected_total,
            written_total=self._written_total,
        )

    async def run(
        self, *, task_status: anyio.abc.TaskStatus[None] = anyio.TASK_STATUS_IGNORED
    ) -> None:
        """Write until cancelled, then make a last, time-boxed attempt to flush."""
        task_status.started()
        backoff = _BACKOFF_START_S
        try:
            while True:
                wrote_full_batch = await self._write_next_batch()
                if self._failing:
                    await anyio.sleep(backoff)
                    backoff = min(backoff * 2, _BACKOFF_MAX_S)
                    continue
                backoff = _BACKOFF_START_S
                if not wrote_full_batch:
                    await anyio.sleep(self._flush_interval_s)
        finally:
            with anyio.CancelScope(shield=True):
                await self.close()

    async def close(self, timeout_s: float = 5.0) -> None:
        """Try to write what is left, within `timeout_s`, then close the connection."""
        with anyio.move_on_after(timeout_s):
            while self._pending or len(self._buffer):
                await self._write_next_batch()
                if self._failing:
                    break
        await self._discard_connection()

    async def _write_next_batch(self) -> bool:
        """Write one batch. Returns whether it was a full one (so more is probably waiting)."""
        if not self._pending:
            self._pending = self._buffer.take(self._batch_size)
        if not self._pending:
            return False
        batch = self._pending
        try:
            with anyio.fail_after(self._write_timeout_s):
                await self._insert(batch)
        except (errors.IntegrityError, errors.DataError):
            if not await self._insert_one_by_one(batch):
                return False  # the split failed: the batch stays queued and the writer degraded
            return len(batch) >= self._batch_size
        except Exception as error:
            await self._discard_connection()
            self._note_failure(error)
            return False
        self._written_total += len(batch)
        self._pending = []
        self._failing = False
        return len(batch) >= self._batch_size

    async def _insert(self, rows: list[Row]) -> None:
        connection = await self._connect()
        async with connection.transaction(), connection.cursor() as cursor:
            by_table: dict[str, list[Row]] = defaultdict(list)
            for row in rows:
                by_table[row.table].append(row)
            for table in TABLES:  # a fixed order, and only tables this module knows
                if table in by_table:
                    await cursor.executemany(_statement(table), _params(table, by_table[table]))

    async def _insert_one_by_one(self, rows: list[Row]) -> bool:
        """After the database refused a batch: write what it accepts, drop what it refuses.

        Returns whether the pass finished. If the database fails part way, the whole batch stays
        pending (rows already written are written again harmlessly: every key is idempotent) and
        the writer is degraded; nothing is counted as written or dropped that was not."""
        refused = 0
        try:
            for row in rows:
                try:
                    with anyio.fail_after(self._write_timeout_s):  # per row, not for the pass
                        await self._insert([row])
                except (errors.IntegrityError, errors.DataError):
                    refused += 1
                    logger.error("the database refused a %s row; it was dropped", row.table)
        except Exception as error:
            await self._discard_connection()
            self._note_failure(error)
            return False
        self._rejected_total += refused
        self._written_total += len(rows) - refused
        self._pending = []
        self._failing = False
        return True

    async def _connect(self) -> AsyncConnection:
        if self._connection is None or self._connection.closed:
            with anyio.fail_after(self._connect_timeout_s + 1):
                self._connection = await AsyncConnection.connect(
                    self._url,
                    connect_timeout=max(1, math.ceil(self._connect_timeout_s)),  # 0 means forever
                    options="-c statement_timeout=3000",
                )
        return self._connection

    async def _discard_connection(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None:
            with anyio.move_on_after(2, shield=True), suppress(psycopg.Error):
                await connection.close()

    def _note_failure(self, error: Exception) -> None:
        self._failing = True
        now = time.monotonic()
        if now - self._last_logged >= _LOG_EVERY_S:
            self._last_logged = now
            # The class, not the message: a driver message can quote a value.
            logger.warning(
                "telemetry writes are failing (%s); %d rows queued, %d dropped so far",
                type(error).__name__,
                len(self._buffer) + len(self._pending),
                self._buffer.dropped_total,
            )


def _statement(table: str) -> sql.Composed:
    columns = COLUMNS[table]
    return sql.SQL("INSERT INTO {}.{} ({}) VALUES ({}) ON CONFLICT DO NOTHING").format(
        sql.Identifier(SCHEMA),
        sql.Identifier(table),
        sql.SQL(", ").join(sql.Identifier(column) for column in columns),
        sql.SQL(", ").join(sql.Placeholder() for _ in columns),
    )


def _params(table: str, rows: list[Row]) -> list[tuple[object, ...]]:
    return [tuple(row.values.get(column) for column in COLUMNS[table]) for row in rows]
