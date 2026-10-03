"""Delete telemetry older than its retention, as the telemetry_purger role.

`telemetry-purge` runs once, or every `--every` seconds. Spans are kept for a shorter time than
the decision records (they are bulkier and the records hold what the dashboard and the
scorecard need). The role can delete rows and read only each table's time column, so rows are
deleted in windows of time rather than by key.
"""

import argparse
import logging
import os
import sys
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta

import anyio
from psycopg import AsyncConnection, sql

from ai_gateway.telemetry import PURGEABLE_TABLES, SCHEMA

logger = logging.getLogger(__name__)

DEFAULT_SPAN_RETENTION_DAYS = 7
DEFAULT_RETENTION_DAYS = 30
_WINDOW = timedelta(hours=1)
"""Rows are deleted this much time at a time, so one statement never covers a whole backlog."""

_URL_ENV = "TELEMETRY_PURGE_DATABASE_URL"
_SPAN_DAYS_ENV = "TELEMETRY_RETENTION_SPAN_DAYS"
_DAYS_ENV = "TELEMETRY_RETENTION_DAYS"


def cutoffs(now: datetime, span_retention_days: int, retention_days: int) -> dict[str, datetime]:
    """The time before which each table's rows are deleted."""
    return {
        table: now - timedelta(days=span_retention_days if table == "spans" else retention_days)
        for table in PURGEABLE_TABLES
    }


async def purge(url: str, cutoff_by_table: dict[str, datetime]) -> dict[str, int]:
    """Delete the rows older than each table's cutoff; return how many went from each."""
    deleted = dict.fromkeys(cutoff_by_table, 0)
    async with await AsyncConnection.connect(url, autocommit=True) as connection:
        for table, cutoff in cutoff_by_table.items():
            deleted[table] = await _purge_table(connection, table, cutoff)
    return deleted


def _window_ends(oldest: datetime, cutoff: datetime) -> Iterator[datetime]:
    """The end of each window from the oldest row up to the cutoff: an hour apart, the last one
    ending at the cutoff itself."""
    end = oldest
    while end < cutoff:
        end = min(cutoff, end + _WINDOW)
        yield end


async def _purge_table(connection: AsyncConnection, table: str, cutoff: datetime) -> int:
    name = sql.Identifier(SCHEMA, table)
    cursor = await connection.execute(sql.SQL("SELECT min(ts) FROM {}").format(name))
    row = await cursor.fetchone()
    oldest = row[0] if row else None
    if oldest is None:
        return 0
    total = 0
    for end in _window_ends(oldest, cutoff):
        cursor = await connection.execute(
            sql.SQL("DELETE FROM {} WHERE ts < %s").format(name), (end,)
        )
        total += cursor.rowcount
    return total


def main(argv: Sequence[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    url = os.environ.get(_URL_ENV)
    if not url:
        sys.exit(f"telemetry-purge: {_URL_ENV} is not set")
    span_days = _days(_SPAN_DAYS_ENV, DEFAULT_SPAN_RETENTION_DAYS)
    days = _days(_DAYS_ENV, DEFAULT_RETENTION_DAYS)
    anyio.run(_run, url, span_days, days, args.every)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="telemetry-purge", description=__doc__)
    parser.add_argument(
        "--every",
        type=_non_negative,
        default=0,
        metavar="SECONDS",
        help="run again this often; 0 (the default) runs once",
    )
    return parser


def _non_negative(raw: str) -> int:
    value = int(raw)
    if value < 0:
        raise argparse.ArgumentTypeError("must be 0 (run once) or a number of seconds")
    return value


def _days(variable: str, default: int) -> int:
    raw = os.environ.get(variable)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value < 1:
        sys.exit(f"telemetry-purge: {variable} must be a whole number of days, at least 1")
    return value


async def _run(url: str, span_days: int, days: int, every: int) -> None:
    while True:
        try:
            deleted = await purge(url, cutoffs(datetime.now(UTC), span_days, days))
            logger.info("telemetry purge deleted %s", deleted)
        except Exception:
            # A purge that fails is retried at the next interval; it never stops the service.
            logger.exception("telemetry purge failed")
            if not every:
                raise
        if not every:
            return
        await anyio.sleep(every)
