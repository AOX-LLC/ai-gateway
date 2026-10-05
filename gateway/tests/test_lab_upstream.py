"""The lab upstream: a deliberately lenient MCP server whose tool definitions can change, for the
red-team scorecard. It is fenced like the lab approver: the `lab` Compose profile, an environment
switch it will not start without, and a credential that exists only when given. Needs no database.
Harborline Supply Co. is fictional, and so is everything the lab server says."""

import json
import tomllib
from pathlib import Path
from typing import Any, cast

import httpx2
import pytest
import yaml
from jsonschema import Draft202012Validator
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp.types import RequestParamsMeta

from ai_gateway.classifier.prompt import split_text
from ai_gateway.pipeline.pins import definition_sha256
from lab_upstream.server import LabNotEnabledError, build_app
from lab_upstream.settings import LabSettings
from lab_upstream.texts import (
    AUDIT_FIRST_HALF,
    AUDIT_SECOND_HALF,
    REPORT_INSTRUCTION,
    long_audit_report,
    long_report,
)
from lab_upstream.tools import PHASES, REVIEWED_TOOLS, definitions
from tests.helpers import serve_in_thread

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "lab-test-token-" + "x" * 24
COMPOSE = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))


def _by_name(phase: str) -> dict[str, Any]:
    return {tool.name: tool for tool in definitions(phase)}


def _hash(tool: Any) -> str:
    return definition_sha256(
        f"lab__{tool.name}", tool.description, tool.input_schema, tool.output_schema
    )


# -- the three phases ----------------------------------------------------------------------------


def test_the_reviewed_phase_offers_exactly_the_reviewed_tools() -> None:
    assert PHASES == ("reviewed", "rugpulled", "poisoned")
    assert set(_by_name("reviewed")) == set(REVIEWED_TOOLS)
    assert set(REVIEWED_TOOLS) == {"fetch_notice", "fetch_report", "fetch_audit", "forward_note"}


def test_a_rug_pull_changes_a_description_and_a_schema_and_nothing_else() -> None:
    reviewed, pulled = _by_name("reviewed"), _by_name("rugpulled")

    assert set(pulled) == set(reviewed)
    changed = {name for name in reviewed if _hash(reviewed[name]) != _hash(pulled[name])}
    assert changed == {"fetch_notice", "forward_note"}
    assert reviewed["fetch_notice"].input_schema == pulled["fetch_notice"].input_schema
    assert "forward_to" not in reviewed["forward_note"].input_schema["properties"]
    assert "forward_to" in pulled["forward_note"].input_schema["properties"]
    assert "customer" in pulled["fetch_notice"].description.lower()


def test_the_poisoned_phase_adds_two_unreviewed_tools_and_changes_none_of_the_reviewed() -> None:
    reviewed, poisoned = _by_name("reviewed"), _by_name("poisoned")

    assert set(poisoned) - set(reviewed) == {"summarize_account", "lookup_record"}
    assert all(_hash(reviewed[name]) == _hash(poisoned[name]) for name in reviewed)
    hidden = poisoned["summarize_account"].description
    assert "​" in hidden, "the instruction is joined with zero-width characters"
    ref = poisoned["lookup_record"].input_schema["properties"]["record_id"]["$ref"]
    assert ref.startswith("http://"), "a remote reference, which the gateway must never fetch"


@pytest.mark.parametrize("phase", ["reviewed", "rugpulled", "poisoned"])
def test_every_definition_is_a_schema_and_carries_the_fictional_notice(phase: str) -> None:
    for tool in definitions(phase):
        Draft202012Validator.check_schema(tool.input_schema)
        if tool.output_schema is not None:
            Draft202012Validator.check_schema(tool.output_schema)
    assert "fictional" in long_report().lower()


def test_an_unknown_phase_is_refused() -> None:
    with pytest.raises(ValueError, match="phase"):
        definitions("evil")


def test_the_reviewed_forward_note_forbids_extra_arguments_and_caps_the_note() -> None:
    schema = _by_name("reviewed")["forward_note"].input_schema

    assert schema["additionalProperties"] is False
    assert schema["properties"]["note"]["maxLength"] == 500
    assert schema["required"] == ["note"]


# -- the long report: the boundary the overlap is for --------------------------------------------


def test_the_instruction_in_the_long_report_straddles_the_old_cut_and_not_the_new_one() -> None:
    text = long_report()

    assert len(text) > 8000
    assert text.count(REPORT_INSTRUCTION) == 1
    legacy = split_text(text, 6000)
    assert len(legacy) >= 2
    assert not any(REPORT_INSTRUCTION in unit for unit in legacy), "cut in two"
    overlapping = split_text(text, 6000, overlap=400)
    assert any(REPORT_INSTRUCTION in unit for unit in overlapping), "whole in one unit"


def test_the_long_report_is_the_same_every_time() -> None:
    assert long_report() == long_report()


# -- the server ----------------------------------------------------------------------------------


def _settings(**values: Any) -> LabSettings:
    base = {"service_token": TOKEN, "mutable_upstream": "yes", "port": 4413}
    return LabSettings(**{**base, **values})


def test_the_server_will_not_start_without_the_switch() -> None:
    for value in ("", "no", "true", "YES "):
        with pytest.raises(LabNotEnabledError, match="LAB_MUTABLE_UPSTREAM"):
            build_app(_settings(mutable_upstream=value))


def test_the_server_will_not_start_with_a_missing_or_weak_credential() -> None:
    from mcp_common.credentials import MissingCredentialError, WeakCredentialError

    with pytest.raises(MissingCredentialError):
        build_app(_settings(service_token=" "))  # noqa: S106
    with pytest.raises(WeakCredentialError):
        build_app(_settings(service_token="short"))  # noqa: S106
    with pytest.raises(WeakCredentialError):
        build_app(_settings(service_token="change-me-" + "x" * 30))


def test_an_unknown_phase_in_the_environment_stops_startup() -> None:
    with pytest.raises(ValueError, match="phase"):
        build_app(_settings(phase="evil"))


@pytest.fixture
def lab_url() -> Any:
    with serve_in_thread(build_app(_settings(allowed_hosts=["127.0.0.1:*"]))) as base:
        yield base


@pytest.fixture
def poisoned_url() -> Any:
    app = build_app(_settings(phase="poisoned", allowed_hosts=["127.0.0.1:*"]))
    with serve_in_thread(app) as base:
        yield base


def _http() -> httpx2.Client:
    return httpx2.Client(headers={"Authorization": f"Bearer {TOKEN}"})


def test_health_is_open_and_everything_else_needs_the_credential(lab_url: str) -> None:
    with httpx2.Client() as bare:
        assert bare.get(f"{lab_url}/healthz").json()["status"] == "ok"
        assert bare.get(f"{lab_url}/effects").status_code == 401
        assert bare.post(f"{lab_url}/effects/reset").status_code == 401
        assert bare.post(f"{lab_url}/mcp", json={}).status_code == 401


async def _call(url: str, name: str, arguments: dict[str, Any]) -> Any:
    try:
        async with (
            httpx2.AsyncClient(headers={"Authorization": f"Bearer {TOKEN}"}) as http,
            Client(streamable_http_client(f"{url}/mcp", http_client=http), mode="legacy") as client,
        ):
            return await client.call_tool(name, arguments)
    except BaseExceptionGroup as group:  # the client's task groups wrap the one error
        error: BaseException = group
        while isinstance(error, BaseExceptionGroup):
            error = error.exceptions[0]
        raise error from None


@pytest.mark.anyio
async def test_a_lenient_write_accepts_what_the_reviewed_schema_forbids_and_counts_it(
    lab_url: str,
) -> None:
    await _call(lab_url, "forward_note", {"note": "fine"})
    await _call(lab_url, "forward_note", {"note": "x" * 600})
    await _call(lab_url, "forward_note", {"note": "n", "forward_to": "somewhere"})

    with _http() as http:
        effects = http.get(f"{lab_url}/effects").json()

    assert effects["phase"] == "reviewed"
    assert effects["calls"] == {"forward_note": 3}
    assert effects["extra_arguments"] == 1
    assert effects["oversized_arguments"] == 1
    assert "somewhere" not in json.dumps(effects), "counts, never a value"


@pytest.mark.anyio
async def test_reads_are_recorded_and_the_log_can_be_reset(lab_url: str) -> None:
    notice = await _call(lab_url, "fetch_notice", {})
    report = await _call(lab_url, "fetch_report", {})

    assert json.loads(notice.content[0].text) == notice.structured_content
    assert len(report.structured_content["report"]) > 8000
    with _http() as http:
        assert http.get(f"{lab_url}/effects").json()["calls"] == {
            "fetch_notice": 1,
            "fetch_report": 1,
        }
        assert http.post(f"{lab_url}/effects/reset").status_code == 200
        assert http.get(f"{lab_url}/effects").json()["calls"] == {}


@pytest.mark.anyio
async def test_a_tool_of_another_phase_is_not_there(lab_url: str) -> None:
    with pytest.raises(MCPError):
        await _call(lab_url, "summarize_account", {"account_id": "ACC-00001"})


@pytest.mark.anyio
async def test_the_poisoned_phase_serves_its_extra_tools(poisoned_url: str) -> None:
    summary = await _call(poisoned_url, "summarize_account", {"account_id": "ACC-00001"})
    await _call(poisoned_url, "lookup_record", {"record_id": "x"})

    assert summary.is_error is False
    with _http() as http:
        calls = http.get(f"{poisoned_url}/effects").json()["calls"]
    assert calls == {"summarize_account": 1, "lookup_record": 1}


# -- the fence: lab profile only, internal network, no host port, in no default image -----------

SERVICE = COMPOSE["services"]["lab-upstream"]
DEFAULT_SERVICES = {n for n, s in COMPOSE["services"].items() if not s.get("profiles")}
DOCKERFILES = {
    "servers/Dockerfile": (ROOT / "servers" / "Dockerfile").read_text(encoding="utf-8"),
    "gateway/Dockerfile": (ROOT / "gateway" / "Dockerfile").read_text(encoding="utf-8"),
    "dashboard/Dockerfile": (ROOT / "dashboard" / "Dockerfile").read_text(encoding="utf-8"),
    "servers/echo/Dockerfile": (ROOT / "servers" / "echo" / "Dockerfile").read_text(
        encoding="utf-8"
    ),
}


def test_the_lab_upstream_is_in_the_lab_profile_alone() -> None:
    assert SERVICE["profiles"] == ["lab"]
    assert "lab-upstream" not in DEFAULT_SERVICES


def test_the_lab_upstream_sits_on_the_internal_network_and_publishes_nothing() -> None:
    assert SERVICE["networks"] == ["backend"]
    assert "ports" not in SERVICE
    assert "expose" not in SERVICE or SERVICE["expose"] == ["4413"]
    on_edge = {n for n, s in COMPOSE["services"].items() if "edge" in s.get("networks", [])}
    assert "lab-upstream" not in on_edge


def test_the_switch_and_the_credential_come_from_the_environment_and_are_never_defaulted_on() -> (
    None
):
    env = SERVICE["environment"]

    assert env["LAB_MUTABLE_UPSTREAM"] == "${LAB_MUTABLE_UPSTREAM:-}"
    assert env["LAB_SERVICE_TOKEN"] == "${LAB_UPSTREAM_TOKEN:-}"  # noqa: S105
    assert env["LAB_PHASE"] == "${LAB_UPSTREAM_PHASE:-reviewed}"
    assert SERVICE["restart"] == "no"
    assert SERVICE["read_only"] is True
    assert SERVICE["cap_drop"] == ["ALL"]


def test_no_default_service_depends_on_it_and_it_has_an_image_of_its_own() -> None:
    for name in DEFAULT_SERVICES:
        assert "lab-upstream" not in COMPOSE["services"][name].get("depends_on", {}), name
    images = {s.get("image") for n, s in COMPOSE["services"].items() if n != "lab-upstream"}
    assert SERVICE["image"] == "ai-gateway-lab-upstream"
    assert SERVICE["image"] not in images
    assert SERVICE["build"]["dockerfile"] == "servers/lab/Dockerfile"


@pytest.mark.parametrize(
    "dockerfile", ["servers/Dockerfile", "gateway/Dockerfile", "dashboard/Dockerfile"]
)
def test_no_default_image_installs_or_copies_the_lab_upstream(dockerfile: str) -> None:
    text = DOCKERFILES[dockerfile]

    assert "lab-upstream" not in text
    assert "lab_upstream" not in text
    assert "servers/lab" not in text


def test_the_servers_image_copies_only_its_environment_into_the_final_stage() -> None:
    final = DOCKERFILES["servers/Dockerfile"].split("\nFROM ")[-1]

    copies = [line for line in final.splitlines() if line.startswith("COPY")]
    assert all("--from=builder /app/.venv" in line or "--from=model" in line for line in copies)


def test_the_lab_image_installs_the_lab_package_alone_and_runs_as_nobody_special() -> None:
    text = (ROOT / "servers" / "lab" / "Dockerfile").read_text(encoding="utf-8")

    assert "--package lab-upstream" in text
    assert text.count("--package") == 1
    assert "USER 10001" in text
    assert "EXPOSE 4413" in text


def test_the_default_environment_file_does_not_switch_the_lab_on_or_carry_its_credential() -> None:
    lines = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    active = [line for line in lines if line.strip() and not line.lstrip().startswith("#")]

    assert not [
        line for line in active if line.startswith(("LAB_MUTABLE_UPSTREAM", "LAB_UPSTREAM_TOKEN"))
    ]


def test_the_lab_upstream_is_not_a_dependency_of_any_other_workspace_member() -> None:
    for pyproject in (ROOT / "servers").glob("*/pyproject.toml"):
        if pyproject.parent.name == "lab":
            continue
        deps = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["dependencies"]
        assert not any("lab-upstream" in dep for dep in deps), pyproject
    gateway = tomllib.loads((ROOT / "gateway" / "pyproject.toml").read_text(encoding="utf-8"))
    assert not any("lab-upstream" in dep for dep in gateway["project"]["dependencies"])


def test_every_variable_compose_gives_the_lab_upstream_is_one_it_reads() -> None:
    """A variable compose sets that the settings class never reads is silently ignored: the phase
    was once set as LAB_UPSTREAM_PHASE while the server read LAB_PHASE, so every run stayed in
    `reviewed`. The host's name for it may differ (`${LAB_UPSTREAM_PHASE}`); the container's must
    be a field of LabSettings under its prefix."""
    fields = set(LabSettings.model_fields)

    for name in SERVICE["environment"]:
        assert name.startswith("LAB_"), name
        assert name.removeprefix("LAB_").lower() in fields, f"{name} is never read"


def test_the_phase_compose_sets_is_the_phase_the_server_runs_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LAB_PHASE", "rugpulled")
    monkeypatch.setenv("LAB_SERVICE_TOKEN", TOKEN)
    monkeypatch.setenv("LAB_MUTABLE_UPSTREAM", "yes")

    assert LabSettings().phase == "rugpulled"
    assert SERVICE["environment"]["LAB_PHASE"] == "${LAB_UPSTREAM_PHASE:-reviewed}"


# -- the second long report: an instruction that exists only once its halves are joined ---------


def test_the_audit_report_splits_a_sentence_across_the_old_cut_and_not_the_new_one() -> None:
    text = long_audit_report()
    joined = f"{AUDIT_FIRST_HALF} {AUDIT_SECOND_HALF}"

    assert len(text) > 8000
    assert text.count(joined) == 1
    legacy = split_text(text, 6000)
    assert len(legacy) >= 2
    assert sum(AUDIT_FIRST_HALF in unit for unit in legacy) == 1
    assert sum(AUDIT_SECOND_HALF in unit for unit in legacy) == 1
    assert not any(joined in unit for unit in legacy), "the old cut falls between the halves"
    assert any(joined in unit for unit in split_text(text, 6000, overlap=400)), "overlap joins them"


def test_neither_half_of_the_audit_sentence_carries_an_override_cue_or_a_reader_directive() -> None:
    """The expectation committed before recording: the instruction is in the joining, not in a
    half. The halves are third-person description of a routine; there is no `ignore`, no
    `instructions`, no imperative verb at the start of a clause."""
    cues = ("ignore", "disregard", "instruction", "forget", "override", "you ", "your ", "please")

    for half in (AUDIT_FIRST_HALF, AUDIT_SECOND_HALF):
        assert not [cue for cue in cues if cue in half.lower()], half
    assert not [cue for cue in cues if cue in long_audit_report().lower()], "nowhere in the report"
    assert AUDIT_SECOND_HALF.split()[0] == "exports", "a statement about the reader, not an order"


def test_the_audit_report_is_the_same_every_time_and_the_first_report_is_unchanged() -> None:
    assert long_audit_report() == long_audit_report()
    assert long_report().count(REPORT_INSTRUCTION) == 1


# -- the effect log, per client: attacks of one phase can run at once, each as its own clone ------


def test_the_effect_log_keeps_a_count_for_each_client_and_never_a_value() -> None:
    from lab_upstream.tools import EffectLog

    log = EffectLog("reviewed")
    log.record("forward_note", {"note": "a"}, "client-one")
    log.record("forward_note", {"note": "x" * 600, "forward_to": "somewhere"}, "client-two")
    log.record("fetch_notice", {}, "client-two")

    snapshot = log.snapshot()

    assert snapshot["calls"] == {"fetch_notice": 1, "forward_note": 2}, "the totals are as before"
    assert snapshot["by_client"]["client-one"] == {
        "calls": {"forward_note": 1},
        "extra_arguments": 0,
        "oversized_arguments": 0,
    }
    assert snapshot["by_client"]["client-two"] == {
        "calls": {"fetch_notice": 1, "forward_note": 1},
        "extra_arguments": 1,
        "oversized_arguments": 1,
    }
    assert "somewhere" not in json.dumps(snapshot)
    log.reset()
    assert log.snapshot()["by_client"] == {}


@pytest.mark.anyio
async def test_the_server_attributes_a_call_to_the_client_the_gateway_names(lab_url: str) -> None:
    from mcp_common.attribution import CLIENT_META_KEY

    async with (
        httpx2.AsyncClient(headers={"Authorization": f"Bearer {TOKEN}"}) as http,
        Client(streamable_http_client(f"{lab_url}/mcp", http_client=http), mode="legacy") as client,
    ):
        named = cast(RequestParamsMeta, {CLIENT_META_KEY: "harborline-lab-bot--demo"})
        await client.call_tool("fetch_notice", {}, meta=named)
        await client.call_tool("fetch_notice", {})

    with _http() as sync:
        by_client = sync.get(f"{lab_url}/effects").json()["by_client"]
    assert by_client["harborline-lab-bot--demo"]["calls"] == {"fetch_notice": 1}
    assert by_client["direct"]["calls"] == {"fetch_notice": 1}
