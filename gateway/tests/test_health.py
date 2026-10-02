import psycopg
import pytest

from ai_gateway.health import BuildIdentity, SchemaVersionCache
from ai_gateway.settings import GatewaySettings


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
    monkeypatch.setenv("GATEWAY_GIT_COMMIT", given)

    assert GatewaySettings().git_commit == expected
