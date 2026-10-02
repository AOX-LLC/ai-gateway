"""Test helpers shared by several test modules."""

import threading
import time
from collections.abc import AsyncGenerator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

import httpx2
import uvicorn
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from pydantic import SecretStr
from starlette.types import ASGIApp

from ai_gateway.app import create_app
from ai_gateway.seams.events import MemoryEventSink
from ai_gateway.settings import GatewaySettings

_SERVER_START_TIMEOUT_S = 10.0


@contextmanager
def serve_in_thread(app: ASGIApp) -> Iterator[str]:
    """Run an ASGI app on a free 127.0.0.1 port in a thread; yield its base URL."""
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + _SERVER_START_TIMEOUT_S
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            server.should_exit = True
            raise RuntimeError("test server did not start")
        time.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=_SERVER_START_TIMEOUT_S)


class RunningGateway:
    def __init__(self, url: str, events: MemoryEventSink) -> None:
        self.url = url
        self.events = events


@contextmanager
def run_gateway(database_url: str, workdir: Path) -> Iterator[RunningGateway]:
    """The whole gateway on a free port, with only the scope layer, recording its events."""
    pipeline_file = workdir / "pipeline.toml"
    pipeline_file.write_text('[layers]\nscope = "enforce"\n')
    events = MemoryEventSink()
    settings = GatewaySettings(database_url=SecretStr(database_url), pipeline_file=pipeline_file)
    with serve_in_thread(create_app(settings, events)) as base_url:
        yield RunningGateway(f"{base_url}/mcp", events)


@asynccontextmanager
async def connect(url: str, token: str) -> AsyncGenerator[Client]:
    """An MCP client of the gateway (or of a server), with its bearer token."""
    headers = {"Authorization": f"Bearer {token}"}
    async with (
        httpx2.AsyncClient(headers=headers) as http_client,
        Client(streamable_http_client(url, http_client=http_client), mode="legacy") as client,
    ):
        yield client
