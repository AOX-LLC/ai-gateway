"""The catalog and the per-session upstream pool, against fake upstream clients."""

from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any, cast
from uuid import uuid4

import anyio
import httpx2
import psycopg
import pytest
from mcp.client import Client
from mcp.shared.exceptions import MCPError
from mcp.types import INVALID_PARAMS, CallToolResult, ListToolsResult, TextContent, Tool

from ai_gateway.pipeline.runner import UpstreamStatus
from ai_gateway.proxy.catalog import Catalog
from ai_gateway.proxy.sessions import (
    SessionOwnershipError,
    UpstreamCallError,
    UpstreamSessionPool,
)
from ai_gateway.registry.models import UpstreamServer

ECHO = UpstreamServer(
    id=uuid4(),
    namespace="echo",
    url="http://echo.test/mcp",
    connect_timeout_s=1,
    call_timeout_s=0.5,
)


class FakeUpstream:
    """Stands in for an MCP server: counts connections and misbehaves on request."""

    def __init__(self, tool_pages: list[list[str]] | None = None) -> None:
        self.tool_pages = tool_pages or [["say", "shout"]]
        self.is_down = False
        self.hangs = False
        self.behaviour = "echo"
        self.attempts = 0
        self.opened = 0
        self.closed = 0
        self.calls: list[str] = []
        self.cancelled = 0

    @asynccontextmanager
    async def open(self, upstream: UpstreamServer) -> AsyncGenerator[Client]:
        self.attempts += 1
        if self.is_down:
            raise httpx2.ConnectError("connection refused")
        if self.hangs:
            await anyio.sleep_forever()
        self.opened += 1
        try:
            yield cast(Client, FakeClient(self))
        finally:
            self.closed += 1


class FakeClient:
    def __init__(self, upstream: FakeUpstream) -> None:
        self._upstream = upstream

    async def list_tools(self, *, cursor: str | None = None) -> ListToolsResult:
        page = int(cursor or 0)
        names = self._upstream.tool_pages[page]
        has_more = page + 1 < len(self._upstream.tool_pages)
        return ListToolsResult(
            tools=[Tool(name=name, input_schema={"type": "object"}) for name in names],
            next_cursor=str(page + 1) if has_more else None,
        )

    async def call_tool(
        self, name: str, arguments: dict[str, Any], read_timeout_seconds: float | None = None
    ) -> CallToolResult:
        upstream = self._upstream
        upstream.calls.append(name)
        if upstream.behaviour == "slow":
            try:
                await anyio.sleep(60)
            except anyio.get_cancelled_exc_class():
                upstream.cancelled += 1
                raise
        if upstream.behaviour == "reject":
            raise MCPError(INVALID_PARAMS, "no such tool")
        if upstream.behaviour == "disconnect":
            raise httpx2.RemoteProtocolError("peer closed connection")
        return CallToolResult(content=[TextContent(type="text", text=arguments["text"])])


class StaticSource:
    def __init__(self, *upstreams: UpstreamServer) -> None:
        self.upstreams = list(upstreams)

    async def enabled_upstreams(self) -> list[UpstreamServer]:
        return self.upstreams


class ManualClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


# Upstream session pool


@asynccontextmanager
async def running_pool(
    upstream: FakeUpstream, **kwargs: Any
) -> AsyncGenerator[UpstreamSessionPool]:
    pool = UpstreamSessionPool(open_client=upstream.open, **kwargs)
    async with anyio.create_task_group() as task_group:
        await task_group.start(pool.run)
        yield pool
        task_group.cancel_scope.cancel()


@pytest.mark.anyio
async def test_each_session_gets_its_own_reused_connection() -> None:
    upstream = FakeUpstream()
    client_a, client_b = uuid4(), uuid4()

    async with running_pool(upstream) as pool:
        for _ in range(3):
            await pool.call_tool("session-a", client_a, ECHO, "say", {"text": "hi"})
        await pool.call_tool("session-b", client_b, ECHO, "say", {"text": "hi"})

        assert upstream.opened == 2
    assert upstream.closed == 2


@pytest.mark.anyio
async def test_ending_a_session_closes_its_connections() -> None:
    upstream = FakeUpstream()
    client_id = uuid4()

    async with running_pool(upstream) as pool:
        await pool.call_tool("session-a", client_id, ECHO, "say", {"text": "hi"})
        pool.close_session("session-a", uuid4())  # another client's DELETE changes nothing
        await anyio.wait_all_tasks_blocked()
        assert upstream.closed == 0

        pool.close_session("session-a", client_id)
        await anyio.wait_all_tasks_blocked()
        assert upstream.closed == 1


@pytest.mark.anyio
async def test_idle_sessions_are_closed() -> None:
    upstream = FakeUpstream()
    clock = ManualClock()

    async with running_pool(upstream, clock=clock, idle_timeout_s=60) as pool:
        await pool.call_tool("session-a", uuid4(), ECHO, "say", {"text": "hi"})
        clock.now += 30
        assert pool.close_idle_sessions() == 0
        clock.now += 31
        assert pool.close_idle_sessions() == 1
        await anyio.wait_all_tasks_blocked()
        assert upstream.closed == 1


@pytest.mark.anyio
async def test_a_session_id_cannot_be_used_by_a_second_client() -> None:
    async with running_pool(FakeUpstream()) as pool:
        await pool.call_tool("session-a", uuid4(), ECHO, "say", {"text": "hi"})

        with pytest.raises(SessionOwnershipError):
            await pool.call_tool("session-a", uuid4(), ECHO, "say", {"text": "hi"})


@pytest.mark.anyio
async def test_a_slow_upstream_times_out_and_the_call_is_cancelled() -> None:
    upstream = FakeUpstream()
    upstream.behaviour = "slow"

    async with running_pool(upstream) as pool:
        with anyio.fail_after(5), pytest.raises(UpstreamCallError) as raised:
            await pool.call_tool("session-a", uuid4(), ECHO, "say", {"text": "hi"})

    assert raised.value.status is UpstreamStatus.TIMEOUT
    assert upstream.cancelled == 1


@pytest.mark.anyio
async def test_a_client_that_goes_away_cancels_the_upstream_call() -> None:
    upstream = FakeUpstream()
    upstream.behaviour = "slow"

    async with running_pool(upstream) as pool, anyio.create_task_group() as task_group:
        task_group.start_soon(pool.call_tool, "session-a", uuid4(), ECHO, "say", {"text": "hi"})
        await anyio.wait_all_tasks_blocked()
        task_group.cancel_scope.cancel()

    assert upstream.cancelled == 1


@pytest.mark.anyio
async def test_an_upstream_rejection_is_reported_not_retried() -> None:
    upstream = FakeUpstream()
    upstream.behaviour = "reject"

    async with running_pool(upstream) as pool:
        with pytest.raises(UpstreamCallError) as raised:
            await pool.call_tool("session-a", uuid4(), ECHO, "say", {"text": "hi"})

    assert raised.value.status is UpstreamStatus.REJECTED
    assert upstream.calls == ["say"]


@pytest.mark.anyio
async def test_a_dropped_connection_is_replaced_on_the_next_call_but_not_retried() -> None:
    upstream = FakeUpstream()
    upstream.behaviour = "disconnect"
    client_id = uuid4()

    async with running_pool(upstream) as pool:
        with pytest.raises(UpstreamCallError) as raised:
            await pool.call_tool("session-a", client_id, ECHO, "say", {"text": "hi"})
        assert raised.value.status is UpstreamStatus.UNAVAILABLE
        assert upstream.calls == ["say"]

        upstream.behaviour = "echo"
        await anyio.wait_all_tasks_blocked()
        result = await pool.call_tool("session-a", client_id, ECHO, "say", {"text": "hi"})

    assert result.content == [TextContent(type="text", text="hi")]
    assert upstream.opened == 2


@pytest.mark.anyio
async def test_an_unreachable_upstream_is_unavailable() -> None:
    upstream = FakeUpstream()
    upstream.is_down = True

    async with running_pool(upstream) as pool:
        with pytest.raises(UpstreamCallError) as raised:
            await pool.call_tool("session-a", uuid4(), ECHO, "say", {"text": "hi"})

    assert raised.value.status is UpstreamStatus.UNAVAILABLE


# Catalog


class FlakyRegistry(StaticSource):
    def __init__(self, *upstreams: UpstreamServer) -> None:
        super().__init__(*upstreams)
        self.is_down = False

    async def enabled_upstreams(self) -> list[UpstreamServer]:
        if self.is_down:
            raise psycopg.OperationalError("server closed the connection unexpectedly")
        return self.upstreams


def _upstream(namespace: str, timeout_s: float = 0.5) -> UpstreamServer:
    return UpstreamServer(
        id=uuid4(),
        namespace=namespace,
        url=f"http://{namespace}.test/mcp",
        connect_timeout_s=timeout_s,
        call_timeout_s=timeout_s,
    )


@asynccontextmanager
async def running_catalog(catalog: Catalog) -> AsyncGenerator[Catalog]:
    async with anyio.create_task_group() as task_group:
        await task_group.start(catalog.run)
        yield catalog
        task_group.cancel_scope.cancel()


async def eventually(condition: Callable[[], bool], timeout_s: float = 5.0) -> None:
    # Polling is the point: the catalog has no event for a test to wait on.
    with anyio.fail_after(timeout_s):
        while not condition():  # noqa: ASYNC110
            await anyio.sleep(0.01)


@pytest.mark.anyio
async def test_catalog_exposes_namespaced_tools_across_pages() -> None:
    upstream = FakeUpstream(tool_pages=[["say"], ["shout", "bad.name", "say"]])
    catalog = Catalog(StaticSource(ECHO), open_client=upstream.open)

    async with running_catalog(catalog):
        assert [tool.exposed_name for tool in catalog.tools()] == ["echo__say", "echo__shout"]
        resolved = catalog.resolve("echo__shout")
        assert resolved is not None
        assert resolved.tool.upstream_name == "shout"
        assert resolved.upstream == ECHO
        assert catalog.resolve("echo__bad.name") is None


@pytest.mark.anyio
async def test_an_unreachable_upstream_drops_its_tools_and_comes_back() -> None:
    upstream = FakeUpstream()
    catalog = Catalog(StaticSource(ECHO), open_client=upstream.open, refresh_interval_s=0.05)

    async with running_catalog(catalog):
        upstream.is_down = True
        await eventually(lambda: catalog.tools() == [])
        assert catalog.resolve("echo__say") is None

        upstream.is_down = False
        await eventually(lambda: len(catalog.tools()) == 2)  # first retry after 1 s


@pytest.mark.anyio
async def test_a_hung_upstream_does_not_hold_up_the_others() -> None:
    hung, healthy = FakeUpstream(), FakeUpstream()
    hung.hangs = True
    stuck = _upstream("stuck", timeout_s=1.0)

    @asynccontextmanager
    async def open_by_namespace(upstream: UpstreamServer) -> AsyncGenerator[Client]:
        chosen = hung if upstream.namespace == "stuck" else healthy
        async with chosen.open(upstream) as client:
            yield client

    catalog = Catalog(
        StaticSource(stuck, ECHO),
        open_client=open_by_namespace,
        refresh_interval_s=0.05,
        startup_wait_s=0.2,
    )

    with anyio.fail_after(1.5):
        async with running_catalog(catalog):
            # The stuck upstream's refresh is still waiting on its 2 s timeout, yet the
            # healthy one has its tools and keeps refreshing on its own schedule.
            assert [tool.namespace for tool in catalog.tools()] == ["echo", "echo"]
            await eventually(lambda: healthy.opened >= 5)
            assert catalog.resolve("stuck__say") is None


@pytest.mark.anyio
async def test_a_removed_upstream_disappears_and_is_no_longer_contacted() -> None:
    upstream = FakeUpstream()
    registry = StaticSource(ECHO)
    catalog = Catalog(
        registry, open_client=upstream.open, refresh_interval_s=0.05, registry_poll_s=0.05
    )

    async with running_catalog(catalog):
        await eventually(lambda: upstream.opened >= 2)
        registry.upstreams = []
        await eventually(lambda: catalog.tools() == [])
        contacts_after_removal = upstream.attempts
        await anyio.sleep(0.3)

        assert upstream.attempts == contacts_after_removal


@pytest.mark.anyio
async def test_a_changed_upstream_is_refreshed_at_its_new_address_only() -> None:
    contacted: list[str] = []
    upstream = FakeUpstream()

    @asynccontextmanager
    async def open_recording(server: UpstreamServer) -> AsyncGenerator[Client]:
        contacted.append(server.url)
        async with upstream.open(server) as client:
            yield client

    moved = replace(ECHO, url="http://echo-moved.test/mcp")
    registry = StaticSource(ECHO)
    catalog = Catalog(
        registry, open_client=open_recording, refresh_interval_s=0.05, registry_poll_s=0.05
    )

    async with running_catalog(catalog):
        registry.upstreams = [moved]
        await eventually(lambda: contacted.count(moved.url) >= 2)
        old_address_contacts = contacted.count(ECHO.url)
        await anyio.sleep(0.3)

        assert contacted.count(ECHO.url) == old_address_contacts
        resolved = catalog.resolve("echo__say")
        assert resolved is not None
        assert resolved.upstream == moved


@pytest.mark.anyio
async def test_a_down_upstream_is_retried_with_backoff_not_hammered() -> None:
    upstream = FakeUpstream()
    upstream.is_down = True
    catalog = Catalog(StaticSource(ECHO), open_client=upstream.open, refresh_interval_s=0.05)

    async with running_catalog(catalog):
        await anyio.sleep(0.9)

    # Retrying every refresh interval would be about 18 attempts; backoff waits 1 s first.
    assert 1 <= upstream.attempts <= 2


@pytest.mark.anyio
async def test_an_upstream_registered_later_is_picked_up_at_the_next_poll() -> None:
    registry = StaticSource(ECHO)
    catalog = Catalog(registry, open_client=FakeUpstream().open, registry_poll_s=0.05)

    async with running_catalog(catalog):
        registry.upstreams.append(_upstream("crm"))
        await eventually(lambda: catalog.resolve("crm__say") is not None)


@pytest.mark.anyio
async def test_a_registry_outage_keeps_the_last_catalog() -> None:
    registry = FlakyRegistry(ECHO)
    catalog = Catalog(registry, open_client=FakeUpstream().open)

    async with running_catalog(catalog):
        registry.is_down = True
        await catalog.sync_with_registry()

        assert [tool.exposed_name for tool in catalog.tools()] == ["echo__say", "echo__shout"]
