"""Test helpers shared by several test modules."""

import json
import threading
import time
from collections.abc import AsyncGenerator, Iterator, Sequence
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

PINS_FILE = Path(__file__).resolve().parents[2] / "config" / "tool_pins.toml"

HANDBOOK_DOCUMENTS = Path(__file__).resolve().parents[2] / "servers" / "handbook" / "documents"
"""The handbook's Markdown documents: a plain repository folder, in no package or image."""

WINDOW_CHARS = 60
WINDOW_STEP = 30


def flatten(text: str) -> str:
    """Whitespace collapsed to single spaces, as a search snippet is."""
    return " ".join(text.split())


def text_windows(text: str) -> list[str]:
    """Every WINDOW_CHARS-long stretch of the flattened text, every WINDOW_STEP characters,
    and the last stretch, so any 400-character snippet of it holds at least one window."""
    flat = flatten(text)
    if len(flat) <= WINDOW_CHARS:
        return [flat] if flat else []
    starts = [*range(0, len(flat) - WINDOW_CHARS + 1, WINDOW_STEP), len(flat) - WINDOW_CHARS]
    return [flat[start : start + WINDOW_CHARS] for start in starts]


def string_values(value: object) -> Iterator[str]:
    """Every string in a JSON-like value, decoded."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from string_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from string_values(item)


def rendered(structured: object) -> str:
    """Structured tool output as text to scan: its JSON (non-ASCII kept as is), then every
    string in it on a line of its own, decoded."""
    return "\n".join([json.dumps(structured, ensure_ascii=False), *string_values(structured)])


def restricted_fingerprints(texts: list[str]) -> list[str]:
    """What proves a tool output carries restricted handbook text: each text whole and every
    window of it. A snippet is a cut of a chunk, so comparing whole chunks would miss any
    chunk longer than the snippet limit."""
    fingerprints = {flatten(text) for text in texts}
    for text in texts:
        fingerprints.update(text_windows(text))
    return sorted(fingerprints)


def leaked(output: str, fingerprints: list[str], headings: Sequence[str] = ()) -> list[str]:
    """The fingerprints that appear in an output, compared with whitespace flattened, and the
    headings that appear as a whole string value (a short heading is a substring of ordinary
    prose, so it only counts as a field of its own)."""
    flat = flatten(output)
    values = {flatten(line) for line in output.splitlines()}
    found = [value for value in fingerprints if value in flat]
    return found + [heading for heading in headings if heading in values]


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


def pins_text_for_tests() -> str:
    """The shipped pin file: the Harborline servers' reviewed tools and the echo test upstream's."""
    return PINS_FILE.read_text(encoding="utf-8")


@contextmanager
def run_gateway(
    database_url: str,
    workdir: Path,
    telemetry_database_url: str | None = None,
    policy_database_url: str | None = None,
    unaudited_writes: bool | None = None,
    approvals: dict[str, float] | None = None,
    allowlist: str = "",
    rate_limits: str = "",
    fast_catalog: bool = False,
    layers: dict[str, str] | None = None,
    pins: str | None = None,
    egress: str | None = None,
    canaries: str | None = None,
) -> Iterator[RunningGateway]:
    """The whole gateway on a free port, with only the scope layer, recording its events (and,
    given a telemetry database, storing them there too).

    `fast_catalog` makes it look for a changed upstream every 0.3 s, for a test that changes one;
    left off, the catalogue polls at its default, which keeps the run quiet.

    Writes are allowed without an audit record unless a policy database is given: a test that is
    not about the audit log should not need one, and a test that is gets the shipped behaviour."""
    pipeline_file = workdir / "pipeline.toml"
    if unaudited_writes is None:
        unaudited_writes = policy_database_url is None
    unaudited = "true" if unaudited_writes else "false"
    # A test that is not about approvals switches the layer off, which a floor layer allows only
    # with the override flag; one that is gets the shipped behaviour and a short hold.
    approval_mode = "enforce" if approvals is not None else "off"
    # The layers a test is not about are off, as they were before they existed; one that is says so.
    modes = {
        "scope": "enforce",
        "approval": approval_mode,
        "schema": "off",
        "pinned_descriptions": "off",
        "egress": "off",
        "canary": "off",
        **(layers or {}),
    }
    layer_lines = "".join(f'{name} = "{mode}"\n' for name, mode in modes.items())
    pipeline_file.write_text(
        f"[layers]\n{layer_lines}\n[safety]\n"
        f"allow_unaudited_writes = {unaudited}\n"
        f"allow_floor_override = {str(approvals is None).lower()}\n"
    )
    allowlist_file = workdir / "allowlist.toml"
    allowlist_file.write_text(allowlist)
    limits_file = workdir / "rate_limits.toml"
    limits_file.write_text(rate_limits)
    pins_file = workdir / "tool_pins.toml"
    pins_file.write_text(pins if pins is not None else pins_text_for_tests())
    egress_file = workdir / "egress.toml"
    egress_file.write_text(egress if egress is not None else "")
    canaries_file = workdir / "canaries.toml"
    canaries_file.write_text(canaries if canaries is not None else 'shape = "CNRYHBL[0-9A-F]{8}"\n')
    roles_file = workdir / "approval_roles.toml"
    roles_file.write_text('[roles_by_action]\necho__shout = "approver"\n')
    catalog = {"catalog_refresh_s": 0.3, "catalog_registry_poll_s": 0.3} if fast_catalog else {}
    events = MemoryEventSink()
    settings = GatewaySettings(
        database_url=SecretStr(database_url),
        argument_hash_key=SecretStr("test-only-argument-hash-key-0123456789"),
        pipeline_file=pipeline_file,
        approval_roles_file=roles_file,
        allowlist_file=allowlist_file,
        rate_limits_file=limits_file,
        tool_pins_file=pins_file,
        egress_file=egress_file,
        canaries_file=canaries_file,
        telemetry_database_url=(
            SecretStr(telemetry_database_url) if telemetry_database_url else None
        ),
        policy_database_url=SecretStr(policy_database_url) if policy_database_url else None,
        telemetry_flush_interval_s=0.05,
        **catalog,  # type: ignore[arg-type]
        **{f"approval_{key}": value for key, value in (approvals or {}).items()},  # type: ignore[arg-type]
    )
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
