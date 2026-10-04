import anyio
import psycopg
import pytest

from ai_gateway.settings import GatewaySettings
from mcp_common.health import BuildIdentity, SchemaVersionCache


class CountingSource:
    def __init__(self) -> None:
        self.version = 2
        self.reads = 0
        self.is_down = False

    async def schema_version(self) -> int:
        self.reads += 1
        if self.is_down:
            raise psycopg.OperationalError("connection refused")
        return self.version


class HangingSource:
    def __init__(self) -> None:
        self.reads = 0

    async def schema_version(self) -> int:
        self.reads += 1
        await anyio.sleep_forever()
        raise AssertionError("sleep_forever returned")


class GatedSource(CountingSource):
    """Answers only once released, so callers can pile up behind one read."""

    def __init__(self) -> None:
        super().__init__()
        self.release = anyio.Event()

    async def schema_version(self) -> int:
        self.reads += 1
        await self.release.wait()
        return self.version


class Clock:
    now = 100.0

    def __call__(self) -> float:
        return self.now


@pytest.mark.anyio
async def test_schema_version_is_zero_padded_and_cached_for_30_seconds() -> None:
    source, clock = CountingSource(), Clock()
    cache = SchemaVersionCache(source, clock=clock)

    assert await cache.current() == "0002"
    source.version = 3
    clock.now += 29
    assert await cache.current() == "0002"
    clock.now += 2
    assert await cache.current() == "0003"
    assert source.reads == 2


@pytest.mark.anyio
async def test_an_unreadable_schema_version_is_null_and_not_retried_every_call() -> None:
    source, clock = CountingSource(), Clock()
    source.is_down = True
    cache = SchemaVersionCache(source, clock=clock)

    assert await cache.current() is None
    assert await cache.current() is None
    assert source.reads == 1


@pytest.mark.anyio
async def test_a_hanging_read_is_null_after_its_timeout_and_cached() -> None:
    source, clock = HangingSource(), Clock()
    cache = SchemaVersionCache(source, read_timeout_s=0.05, clock=clock)

    with anyio.fail_after(2):
        first = await cache.current()
        second = await cache.current()

    assert (first, second) == (None, None)
    assert source.reads == 1


@pytest.mark.anyio
async def test_the_default_read_timeout_is_two_seconds() -> None:
    assert SchemaVersionCache(CountingSource()).read_timeout_s == 2.0


@pytest.mark.anyio
async def test_concurrent_callers_share_one_read() -> None:
    source, clock = GatedSource(), Clock()
    cache = SchemaVersionCache(source, clock=clock)
    answers: list[str | None] = []

    async def ask() -> None:
        answers.append(await cache.current())

    async with anyio.create_task_group() as task_group:
        for _ in range(10):
            task_group.start_soon(ask)
        await anyio.wait_all_tasks_blocked()
        source.release.set()

    assert answers == ["0002"] * 10
    assert source.reads == 1


@pytest.mark.anyio
async def test_callers_waiting_behind_a_hanging_read_all_get_null_after_one_timeout() -> None:
    source, clock = HangingSource(), Clock()
    cache = SchemaVersionCache(source, read_timeout_s=0.05, clock=clock)
    answers: list[str | None] = []

    async def ask() -> None:
        answers.append(await cache.current())

    with anyio.fail_after(2):
        async with anyio.create_task_group() as task_group:
            for _ in range(5):
                task_group.start_soon(ask)

    assert answers == [None] * 5
    assert source.reads == 1


def test_payload_follows_the_identity_format() -> None:
    payload = BuildIdentity(commit=None, branch=None, version="0.1.0").payload("ok", "0002")

    assert payload["commit"] is None
    assert payload["commit_source"] == "process_start"
    assert payload["schema_version"] == "0002"
    assert list(payload) == [
        "status",
        "commit",
        "commit_source",
        "branch",
        "version",
        "schema_version",
        "uptime_s",
    ]


@pytest.mark.parametrize(("given", "expected"), [("", None), ("abc1234", "abc1234")])
def test_an_empty_build_argument_means_unknown(
    monkeypatch: pytest.MonkeyPatch, given: str, expected: str | None
) -> None:
    monkeypatch.setenv("GATEWAY_DATABASE_URL", "postgresql://unused")
    monkeypatch.setenv("GATEWAY_ARGUMENT_HASH_KEY", "test-only-argument-hash-key-0123456789")
    monkeypatch.setenv("GATEWAY_GIT_COMMIT", given)

    assert GatewaySettings().git_commit == expected
