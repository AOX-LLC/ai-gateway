"""The policy database settings, and the thread pool the audit log is kept out of."""

import asyncio
import threading
from urllib.parse import parse_qs, urlsplit

import anyio
import pytest
from pydantic import SecretStr

from ai_gateway.policy import policy_url
from ai_gateway.policy.database import BoundedPostgresDatabase
from tests.test_upstreams import eventually

URL = "postgresql://policy_gateway:pw@postgres:5432/ai_gateway"


def _options(url: str) -> str:
    return parse_qs(urlsplit(url).query)["options"][0]


def test_the_policy_schema_comes_first_and_nothing_else_is_set_without_being_asked() -> None:
    url = policy_url(URL)

    assert _options(url) == "-c search_path=policy"
    assert "connect_timeout" not in url
    assert "%20" in url, "libpq decodes %20 in a URL's query, not +"


def test_the_database_gives_up_a_wait_before_the_caller_stops_waiting() -> None:
    url = policy_url(URL, lock_timeout_ms=1500, statement_timeout_ms=1800, connect_timeout_s=2)

    assert _options(url) == ("-c search_path=policy -c lock_timeout=1500 -c statement_timeout=1800")
    assert parse_qs(urlsplit(url).query)["connect_timeout"] == ["2"]


def test_the_whole_transaction_is_bounded_too() -> None:
    url = policy_url(URL, transaction_timeout_ms=1900)

    assert _options(url) == "-c search_path=policy -c transaction_timeout=1900"


@pytest.mark.anyio
async def test_a_caller_stops_waiting_for_a_blocked_worker_thread_when_its_time_is_up() -> None:
    release = threading.Event()
    database = BoundedPostgresDatabase(SecretStr(URL), concurrency=1)
    database.run_sync = lambda work, write=False: release.wait(5)  # type: ignore[method-assign,assignment,misc]
    started = anyio.current_time()
    try:
        with anyio.move_on_after(0.2) as scope:
            await database.run(lambda session: None)
        assert scope.cancelled_caught
        assert anyio.current_time() - started < 1.0, "the wait ended at its limit, not the thread's"
        assert database.busy, "the abandoned thread still holds its worker until it returns"
    finally:
        release.set()


def test_other_parts_of_the_url_survive_unchanged() -> None:
    url = policy_url(URL + "?application_name=a+b&sslmode=&connect_timeout=9")

    query = parse_qs(urlsplit(url).query, keep_blank_values=True)
    assert query["application_name"] == ["a+b"], "libpq does not decode +"
    assert query["sslmode"] == [""]
    assert query["connect_timeout"] == ["9"], "kept unless a timeout is asked for"


def test_options_already_in_the_url_are_kept() -> None:
    url = policy_url(URL + "?options=-c%20application_name%3Dgateway&sslmode=prefer")

    assert _options(url) == "-c application_name=gateway -c search_path=policy"
    assert parse_qs(urlsplit(url).query)["sslmode"] == ["prefer"]


@pytest.mark.anyio
async def test_a_stalled_audit_log_fills_its_own_workers_and_not_the_default_thread_pool() -> None:
    database = BoundedPostgresDatabase(SecretStr(URL), concurrency=2)
    release = threading.Event()
    database.run_sync = lambda work, write=False: release.wait(10)  # type: ignore[assignment,method-assign,misc]

    async with anyio.create_task_group() as tasks:
        for _ in range(4):
            tasks.start_soon(database.run, lambda session: None)
        await eventually(lambda: database.busy, timeout_s=3)

        # The loop's default executor, which resolves names for psycopg and httpx, is untouched.
        with anyio.fail_after(1):
            assert await asyncio.get_running_loop().run_in_executor(None, lambda: "free") == "free"

        release.set()
