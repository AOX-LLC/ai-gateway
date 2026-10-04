"""The retention purge: what it deletes, what it keeps, and the role it runs as."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest

from ai_gateway.telemetry.purge import (
    DEFAULT_RETENTION_DAYS,
    DEFAULT_SPAN_RETENTION_DAYS,
    _window_ends,
    cutoffs,
    main,
    purge,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)


def test_spans_are_kept_for_seven_days_and_everything_else_for_thirty() -> None:
    assert (DEFAULT_SPAN_RETENTION_DAYS, DEFAULT_RETENTION_DAYS) == (7, 30)

    by_table = cutoffs(NOW, DEFAULT_SPAN_RETENTION_DAYS, DEFAULT_RETENTION_DAYS)

    assert by_table["spans"] == NOW - timedelta(days=7)
    assert {t: c for t, c in by_table.items() if t != "spans"} == dict.fromkeys(
        ("requests", "layer_verdicts", "auth_failures"), NOW - timedelta(days=30)
    )


def test_a_backlog_is_deleted_in_windows_of_an_hour_ending_at_the_cutoff() -> None:
    oldest = NOW - timedelta(days=40)

    ends = list(_window_ends(oldest, oldest + timedelta(hours=2, minutes=30)))

    assert ends == [
        oldest + timedelta(hours=1),
        oldest + timedelta(hours=2),
        oldest + timedelta(hours=2, minutes=30),
    ]
    assert list(_window_ends(NOW, NOW)) == []
    assert list(_window_ends(NOW, NOW - timedelta(days=1))) == []


@pytest.mark.parametrize("every", ["-1", "-3600"])
def test_a_negative_interval_is_refused(every: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEMETRY_PURGE_DATABASE_URL", "postgresql://x:y@127.0.0.1:1/z")

    with pytest.raises(SystemExit):
        main(["--every", every])


async def _seed(url: str, age_days: float, tag: int) -> None:
    ts = NOW - timedelta(days=age_days)
    request_id = uuid4()
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as connection:
        await connection.execute(
            "INSERT INTO telemetry.requests (request_id, ts, kind, outcome, duration_ms)"
            " VALUES (%s, %s, 'tool_call', 'forwarded', 1)",
            (request_id, ts),
        )
        await connection.execute(
            "INSERT INTO telemetry.layer_verdicts"
            " (request_id, ordinal, ts, layer, hook, mode, verdict)"
            " VALUES (%s, 0, %s, 'scope', 'before_call', 'enforce', 'allow')",
            (request_id, ts),
        )
        await connection.execute(
            "INSERT INTO telemetry.auth_failures (event_id, ts, reason) VALUES (%s, %s, 'missing')",
            (uuid4(), ts),
        )
        await connection.execute(
            "INSERT INTO telemetry.spans (trace_id, span_id, ts, name, duration_us, status)"
            " VALUES (%s, %s, %s, 'gateway.tool_call', 1, 'ok')",
            (f"{tag:032x}", f"{tag:016x}", ts),
        )


async def _counts(url: str) -> dict[str, int]:
    counts = {}
    async with await psycopg.AsyncConnection.connect(url) as connection:
        for table in ("requests", "layer_verdicts", "auth_failures", "spans"):
            cursor = await connection.execute(f"SELECT count(*) FROM telemetry.{table}".encode())
            row = await cursor.fetchone()
            assert row is not None
            counts[table] = int(row[0])
    return counts


async def test_old_rows_go_and_recent_rows_stay_with_each_tables_own_retention(
    telemetry: None, test_database_url: str, purger_url: str
) -> None:
    # 1 day old: kept everywhere. 10 days: past the span retention only. 40 days: gone everywhere.
    for tag, age in enumerate((1, 10, 40, 40.5, 90), start=1):
        await _seed(test_database_url, age, tag)

    deleted = await purge(purger_url, cutoffs(NOW, 7, 30))

    assert deleted == {"requests": 3, "layer_verdicts": 3, "auth_failures": 3, "spans": 4}
    assert await _counts(test_database_url) == {
        "requests": 2,
        "layer_verdicts": 2,
        "auth_failures": 2,
        "spans": 1,
    }


async def test_a_second_purge_deletes_nothing_more(
    telemetry: None, test_database_url: str, purger_url: str
) -> None:
    await _seed(test_database_url, 60, 1)
    await purge(purger_url, cutoffs(NOW, 7, 30))

    deleted = await purge(purger_url, cutoffs(NOW, 7, 30))

    assert set(deleted.values()) == {0}


async def test_a_backlog_spread_over_many_windows_is_deleted_in_steps(
    telemetry: None, test_database_url: str, purger_url: str
) -> None:
    for tag in range(1, 31):  # 30 rows, each a day older than the last, all past retention
        await _seed(test_database_url, 31 + tag, tag)

    deleted = await purge(purger_url, cutoffs(NOW, 7, 30))

    assert deleted["requests"] == 30
    assert (await _counts(test_database_url))["requests"] == 0
