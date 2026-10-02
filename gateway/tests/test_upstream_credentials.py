"""The service credential the gateway sends to an upstream that requires one."""

import logging
from collections.abc import AsyncGenerator, Iterator
from contextlib import asynccontextmanager
from uuid import uuid4

import anyio
import pytest
from pydantic import BaseModel

from ai_gateway.proxy.catalog import Catalog
from ai_gateway.proxy.sessions import UpstreamSessionPool
from ai_gateway.proxy.upstream_client import UpstreamCredentialError, upstream_headers
from ai_gateway.registry.models import UpstreamServer
from mcp_common.http_app import build_mcp_app
from mcp_common.toolset import CallInfo, NoArguments, StrictToolset
from tests.helpers import serve_in_thread
from tests.test_upstreams import StaticSource, eventually, running_catalog

pytestmark = pytest.mark.anyio

CREDENTIAL = "credential-value-for-tests-only"
ENV_NAME = "TEST_UPSTREAM_CREDENTIAL"


class PongOutput(BaseModel):
    pong: bool


async def _ping(arguments: NoArguments, info: CallInfo) -> PongOutput:
    return PongOutput(pong=True)


@pytest.fixture(scope="module")
def guarded_url() -> Iterator[str]:
    """A real MCP server that requires CREDENTIAL, served over HTTP."""
    toolset = StrictToolset()
    toolset.register("ping", "Answer.", NoArguments, PongOutput, _ping, read_only=True)
    server = toolset.build_server("guarded", "0", "Fictional test server.")
    app = build_mcp_app(server, allowed_hosts=["127.0.0.1:*"], credential=CREDENTIAL)
    with serve_in_thread(app) as base_url:
        yield f"{base_url}/mcp"


@asynccontextmanager
async def _running_pool() -> AsyncGenerator[UpstreamSessionPool]:
    pool = UpstreamSessionPool()
    async with anyio.create_task_group() as task_group:
        await task_group.start(pool.run)
        yield pool
        task_group.cancel_scope.cancel()


def _upstream(url: str, credential_env: str | None = ENV_NAME) -> UpstreamServer:
    return UpstreamServer(
        id=uuid4(),
        namespace="guarded",
        url=url,
        connect_timeout_s=2,
        call_timeout_s=5,
        credential_env=credential_env,
    )


def test_no_credential_env_means_no_header() -> None:
    assert upstream_headers(_upstream("http://x.test/mcp", None)) == {}


def test_the_header_carries_the_value_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_NAME, CREDENTIAL)

    assert upstream_headers(_upstream("http://x.test/mcp")) == {
        "Authorization": f"Bearer {CREDENTIAL}"
    }


@pytest.mark.parametrize("value", [None, ""])
def test_a_missing_credential_is_an_error_that_names_the_variable_only(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    if value is None:
        monkeypatch.delenv(ENV_NAME, raising=False)
    else:
        monkeypatch.setenv(ENV_NAME, value)

    with pytest.raises(UpstreamCredentialError, match=ENV_NAME):
        upstream_headers(_upstream("http://x.test/mcp"))


async def test_the_catalog_and_calls_authenticate_with_the_credential(
    guarded_url: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv(ENV_NAME, CREDENTIAL)
    upstream = _upstream(guarded_url)
    catalog = Catalog(StaticSource(upstream))

    async with running_catalog(catalog), _running_pool() as pool:
        await eventually(lambda: catalog.resolve("guarded__ping") is not None)
        result = await pool.call_tool("session-a", uuid4(), upstream, "ping", {}, "some-client")

    assert result.structured_content == {"pong": True}
    assert CREDENTIAL not in caplog.text


async def test_a_wrong_credential_leaves_the_upstream_unavailable(
    guarded_url: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv(ENV_NAME, "not-the-credential")
    catalog = Catalog(StaticSource(_upstream(guarded_url)))

    async with running_catalog(catalog):
        assert catalog.tools() == []

    assert "not-the-credential" not in caplog.text


async def test_a_missing_credential_variable_makes_the_upstream_unavailable_with_one_warning(
    guarded_url: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv(ENV_NAME, raising=False)
    caplog.set_level(logging.WARNING)
    catalog = Catalog(StaticSource(_upstream(guarded_url)), refresh_interval_s=0.05)

    async with running_catalog(catalog):
        assert catalog.tools() == []

    warnings = [r for r in caplog.records if "is unavailable" in r.getMessage()]
    assert len(warnings) == 1
    assert ENV_NAME in caplog.text
