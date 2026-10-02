"""Read/write classification of tools: from the reviewed policy, never from upstream hints."""

import logging
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from mcp.types import CallToolResult, TextContent, ToolAnnotations

from ai_gateway.pipeline.config import PipelineConfig
from ai_gateway.pipeline.runner import Pipeline, UpstreamOutcome, UpstreamStatus
from ai_gateway.pipeline.types import CallContext, ClientIdentity, Effect, EffectSource, ToolCall
from ai_gateway.proxy.catalog import Catalog
from ai_gateway.registry.tool_policies import ToolPolicyFileError, load_tool_policies
from ai_gateway.seams.events import MemoryEventSink
from tests.test_upstreams import ECHO, FakeUpstream, StaticSource, eventually, running_catalog

pytestmark = pytest.mark.anyio

REPO_ROOT = Path(__file__).resolve().parents[2]


def _catalog(upstream: FakeUpstream, source: StaticSource, **kwargs: float) -> Catalog:
    return Catalog(source, open_client=upstream.open, **kwargs)


async def test_a_tool_takes_the_effect_of_its_policy_row() -> None:
    source = StaticSource(ECHO)
    source.policies = {("echo", "say"): "read", ("echo", "shout"): "write"}

    async with running_catalog(_catalog(FakeUpstream(), source)) as catalog:
        say, shout = catalog.resolve("echo__say"), catalog.resolve("echo__shout")

    assert say is not None
    assert shout is not None
    assert (say.tool.effect, say.tool.effect_source) == ("read", "policy")
    assert (shout.tool.effect, shout.tool.effect_source) == ("write", "policy")


async def test_a_tool_without_a_policy_is_a_write_by_default() -> None:
    source = StaticSource(ECHO)
    source.policies = {("echo", "say"): "read"}

    async with running_catalog(_catalog(FakeUpstream(), source)) as catalog:
        shout = catalog.resolve("echo__shout")

    assert shout is not None
    assert (shout.tool.effect, shout.tool.effect_source) == ("write", "default")


async def test_an_upstream_that_claims_read_only_is_still_a_write_without_a_policy() -> None:
    upstream = FakeUpstream()
    upstream.annotations = {"say": ToolAnnotations(read_only_hint=True)}

    async with running_catalog(_catalog(upstream, StaticSource(ECHO))) as catalog:
        say = catalog.resolve("echo__say")

    assert say is not None
    assert (say.tool.effect, say.tool.effect_source) == ("write", "default")
    assert say.tool.tool.annotations is not None
    assert say.tool.tool.annotations.read_only_hint is False


async def test_a_contradicting_hint_is_ignored_and_warned_about_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    upstream = FakeUpstream()
    upstream.annotations = {
        "say": ToolAnnotations(read_only_hint=False),  # policy says read
        "shout": ToolAnnotations(read_only_hint=True),  # policy says write
    }
    source = StaticSource(ECHO)
    source.policies = {("echo", "say"): "read", ("echo", "shout"): "write"}

    async with running_catalog(_catalog(upstream, source, refresh_interval_s=0.05)) as catalog:
        await eventually(lambda: upstream.opened >= 4)
        say, shout = catalog.resolve("echo__say"), catalog.resolve("echo__shout")

    assert say is not None
    assert shout is not None
    assert (say.tool.effect, shout.tool.effect) == ("read", "write")
    drift = [r.getMessage() for r in caplog.records if "readOnlyHint" in r.getMessage()]
    assert len(drift) == 2
    assert any("echo__say" in message for message in drift)
    assert any("echo__shout" in message for message in drift)


async def test_a_hint_that_agrees_with_the_policy_logs_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    upstream = FakeUpstream()
    upstream.annotations = {"say": ToolAnnotations(read_only_hint=True)}
    source = StaticSource(ECHO)
    source.policies = {("echo", "say"): "read"}

    async with running_catalog(_catalog(upstream, source)):
        pass

    assert "readOnlyHint" not in caplog.text


async def test_a_policy_change_applies_at_the_next_registry_poll() -> None:
    source = StaticSource(ECHO)
    source.policies = {("echo", "say"): "read"}

    async with running_catalog(_catalog(FakeUpstream(), source, registry_poll_s=0.05)) as catalog:
        source.policies = {("echo", "say"): "write"}

        def is_write() -> bool:
            resolved = catalog.resolve("echo__say")
            return resolved is not None and resolved.tool.effect == "write"

        await eventually(is_write)


# --- decision records ---------------------------------------------------------------------


def _context() -> CallContext:
    identity = ClientIdentity(
        id=uuid4(), name="harborline-ops-bot", scopes=frozenset({"echo__say"})
    )
    return CallContext(request_id=uuid4(), client=identity, session_id=None, protocol_version="x")


@pytest.mark.parametrize(
    ("effect", "source"), [("read", "policy"), ("write", "policy"), ("write", "default")]
)
async def test_the_decision_record_carries_the_effect(effect: str, source: str) -> None:
    events = MemoryEventSink()
    config = PipelineConfig(modes={}, allow_floor_override=False, sha256="x")
    pipeline = Pipeline([], config, events)
    call = ToolCall.create(
        "echo__say", "echo", "say", {}, cast(Effect, effect), cast(EffectSource, source)
    )

    async def forward(ctx: CallContext, approved: ToolCall) -> UpstreamOutcome:
        result = CallToolResult(content=[TextContent(type="text", text="ok")])
        return UpstreamOutcome(result, UpstreamStatus.OK)

    await pipeline.call_tool(_context(), call, forward)

    payload = events.events[-1].payload
    assert (payload["effect"], payload["effect_source"]) == (effect, source)


def test_a_call_with_no_classification_is_a_default_write() -> None:
    call = ToolCall.create("echo__say", "echo", "say", {})

    assert (call.effect, call.effect_source) == ("write", "default")


# --- the policy file ----------------------------------------------------------------------


def test_the_committed_policy_file_classifies_the_six_ticketing_tools() -> None:
    policies = {
        (p.namespace, p.tool): p.effect
        for p in load_tool_policies(REPO_ROOT / "config" / "tool_policies.toml")
    }

    assert policies == {
        ("tickets", "list_tickets"): "read",
        ("tickets", "get_ticket"): "read",
        ("tickets", "create_ticket"): "write",
        ("tickets", "add_comment"): "write",
        ("tickets", "change_status"): "write",
        ("tickets", "assign"): "write",
    }


@pytest.mark.parametrize(
    "content",
    [
        '[tickets.x]\neffect = "maybe"\n',
        '[tickets.x]\nnotes = "no effect"\n',
        '[tickets.x]\neffect = "read"\nreviewer = "someone"\n',
        '[Tickets.x]\neffect = "read"\n',
        'tickets = "read"\n',
        "not toml [",
    ],
)
def test_a_malformed_policy_file_is_rejected(tmp_path: Path, content: str) -> None:
    path = tmp_path / "policies.toml"
    path.write_text(content)

    with pytest.raises(ToolPolicyFileError):
        load_tool_policies(path)


async def test_clients_see_only_the_gateways_read_only_hint_not_the_upstreams_claims() -> None:
    upstream = FakeUpstream()
    upstream.annotations = {
        "say": ToolAnnotations(
            title="Totally Safe Tool",
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        )
    }
    source = StaticSource(ECHO)
    source.policies = {("echo", "say"): "write"}

    async with running_catalog(_catalog(upstream, source)) as catalog:
        say = next(t for t in catalog.tools() if t.exposed_name == "echo__say")

    assert say.tool.annotations is not None
    assert say.tool.annotations.model_dump(exclude_none=True) == {"read_only_hint": False}
    assert say.tool.title is None
