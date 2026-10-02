"""The tool catalog: every enabled upstream's tools under their exposed names, cached.

tools/list is answered from this cache only and never waits on an upstream. A background
task refreshes each upstream on a short-lived connection, every refresh interval when
it is healthy and with exponential backoff when it is not. A failed refresh makes the
upstream unavailable: its tools disappear until it answers again, so clients never see
stale descriptions for a server that may have changed.
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

import anyio
from anyio.abc import TaskStatus
from mcp.types import Tool

from ai_gateway.pipeline.types import CatalogTool
from ai_gateway.proxy.naming import expose
from ai_gateway.proxy.upstream_client import UpstreamClientFactory, open_upstream_client
from ai_gateway.registry.models import UpstreamServer

logger = logging.getLogger(__name__)

_MAX_LIST_PAGES = 100
_MAX_BACKOFF_S = 60.0


class UpstreamSource(Protocol):
    async def enabled_upstreams(self) -> list[UpstreamServer]: ...


@dataclass
class _UpstreamState:
    upstream: UpstreamServer
    tools: tuple[CatalogTool, ...] = ()
    is_available: bool = False
    consecutive_failures: int = 0
    next_refresh_at: float = 0.0


@dataclass(frozen=True)
class ResolvedTool:
    tool: CatalogTool
    upstream: UpstreamServer


@dataclass
class Catalog:
    source: UpstreamSource
    open_client: UpstreamClientFactory = open_upstream_client
    refresh_interval_s: float = 60.0
    clock: Callable[[], float] = time.monotonic
    _states: dict[str, _UpstreamState] = field(default_factory=dict, init=False)

    def tools(self) -> list[CatalogTool]:
        return [tool for state in self._available_states() for tool in state.tools]

    def resolve(self, exposed_name: str) -> ResolvedTool | None:
        for state in self._available_states():
            for tool in state.tools:
                if tool.exposed_name == exposed_name:
                    return ResolvedTool(tool, state.upstream)
        return None

    async def run(self, *, task_status: TaskStatus[None] = anyio.TASK_STATUS_IGNORED) -> None:
        """Refresh forever. Reports started after the first pass, so a fresh gateway
        has its catalog before it serves the first request."""
        await self.refresh_due()
        task_status.started()
        while True:
            await anyio.sleep(self._seconds_until_next_due())
            await self.refresh_due()

    async def refresh_due(self) -> None:
        """Re-read the upstream registry, then refresh every upstream that is due."""
        await self._sync_with_registry()
        now = self.clock()
        due = [state for state in self._states.values() if state.next_refresh_at <= now]
        async with anyio.create_task_group() as task_group:
            for state in due:
                task_group.start_soon(self._refresh, state)

    async def _sync_with_registry(self) -> None:
        upstreams = {
            upstream.namespace: upstream for upstream in await self.source.enabled_upstreams()
        }
        for namespace in self._states.keys() - upstreams.keys():
            logger.info("upstream %s was removed or disabled", namespace)
            del self._states[namespace]
        for namespace, upstream in upstreams.items():
            known = self._states.get(namespace)
            if known is None or known.upstream != upstream:
                self._states[namespace] = _UpstreamState(upstream)

    async def _refresh(self, state: _UpstreamState) -> None:
        upstream = state.upstream
        try:
            with anyio.fail_after(upstream.connect_timeout_s + upstream.call_timeout_s):
                tools = await self._fetch_tools(upstream)
        except Exception:
            # Whatever went wrong, a server that cannot list its tools is unavailable.
            # Catching broadly is deliberate: this background task must keep running.
            state.consecutive_failures += 1
            backoff_s = min(_MAX_BACKOFF_S, 2.0 ** (state.consecutive_failures - 1))
            state.next_refresh_at = self.clock() + backoff_s
            if state.is_available or state.consecutive_failures == 1:
                logger.warning(
                    "upstream %s is unavailable; retrying in %.0fs",
                    upstream.namespace,
                    backoff_s,
                    exc_info=True,
                )
            state.is_available = False
            state.tools = ()
            return

        if not state.is_available:
            logger.info("upstream %s is available with %d tools", upstream.namespace, len(tools))
        state.tools = tools
        state.is_available = True
        state.consecutive_failures = 0
        state.next_refresh_at = self.clock() + self.refresh_interval_s

    async def _fetch_tools(self, upstream: UpstreamServer) -> tuple[CatalogTool, ...]:
        exposed_tools: dict[str, CatalogTool] = {}
        async with self.open_client(upstream) as client:
            cursor: str | None = None
            for _ in range(_MAX_LIST_PAGES):
                page = await client.list_tools(cursor=cursor)
                for tool in page.tools:
                    self._add_tool(exposed_tools, upstream.namespace, tool)
                cursor = page.next_cursor
                if cursor is None:
                    break
        return tuple(exposed_tools.values())

    @staticmethod
    def _add_tool(exposed_tools: dict[str, CatalogTool], namespace: str, tool: Tool) -> None:
        exposed_name = expose(namespace, tool.name)
        if exposed_name is None:
            logger.warning("skipping tool %r from %s: name cannot be exposed", tool.name, namespace)
            return
        if exposed_name in exposed_tools:
            logger.warning("skipping duplicate tool %r from %s", tool.name, namespace)
            return
        exposed_tools[exposed_name] = CatalogTool(
            namespace=namespace,
            upstream_name=tool.name,
            tool=tool.model_copy(update={"name": exposed_name}),
        )

    def _available_states(self) -> list[_UpstreamState]:
        return [state for state in self._states.values() if state.is_available]

    def _seconds_until_next_due(self) -> float:
        if not self._states:
            return self.refresh_interval_s
        earliest = min(state.next_refresh_at for state in self._states.values())
        return max(0.5, earliest - self.clock())
