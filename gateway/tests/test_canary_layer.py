"""The canary layer: a seeded decoy value in a call's arguments is blocked and alerted."""

import base64
from pathlib import Path

import pytest

from ai_gateway.pipeline.alerts import Alerts
from ai_gateway.pipeline.layers.canary import (
    CanaryConfigError,
    CanaryLayer,
    canary_sha256,
    load_canary_config,
)
from ai_gateway.pipeline.runner import Blocked
from ai_gateway.pipeline.types import Allow, Deny, DenyCode
from ai_gateway.seams.events import GatewayEvent, MemoryEventSink
from crm_server.seed import CRM_CANARY
from crm_server.seed import build_dataset as crm_dataset
from tests.layer_helpers import call, ctx, last_layer, pipeline_with, run
from ticketing_server.seed import TICKETING_CANARY
from ticketing_server.seed import build_dataset as ticketing_dataset

pytestmark = pytest.mark.anyio
ROOT = Path(__file__).resolve().parents[2]
CONFIG = load_canary_config(ROOT / "config" / "canaries.toml")


class Recorder:
    def __init__(self) -> None:
        self.events: list[GatewayEvent] = []

    def record(self, event: GatewayEvent) -> None:
        self.events.append(event)


def _layer() -> tuple[CanaryLayer, Recorder]:
    recorder = Recorder()
    return CanaryLayer(CONFIG, Alerts(recorder)), recorder  # type: ignore[arg-type]


def _write(text: str):  # type: ignore[no-untyped-def]
    return call({"subject": "s", "description": text})


async def test_the_shipped_canaries_are_the_ones_in_the_seeded_data_and_in_the_file() -> None:
    assert canary_sha256(CRM_CANARY) in CONFIG.by_sha256
    assert canary_sha256(TICKETING_CANARY) in CONFIG.by_sha256
    assert CRM_CANARY in crm_dataset().accounts[0].about, "readable through get_account"
    assert TICKETING_CANARY in ticketing_dataset().tickets[0].description
    text = (ROOT / "config" / "canaries.toml").read_text()
    assert CRM_CANARY not in text, "the file holds hashes, never the values"
    assert TICKETING_CANARY not in text


@pytest.mark.parametrize(
    "disguise",
    [
        lambda v: f"the code is {v}",
        lambda v: v.lower(),
        lambda v: v.replace("-", " "),
        lambda v: v.replace("-", "​-​"),
        lambda v: "-".join(v),
        lambda v: base64.b64encode(f"code: {v}".encode()).decode(),
        lambda v: f"x{v.encode().hex()}x".replace("x", ""),
        lambda v: "\uff23\uff2e\uff32\uff39-HBL-3F9A71C2",
    ],
    ids=["plain", "lower", "spaces", "zero-width", "spread", "base64", "hex", "fullwidth"],
)
async def test_a_canary_is_found_however_it_is_dressed(disguise) -> None:  # type: ignore[no-untyped-def]
    layer, _ = _layer()

    verdict = await layer.before_call(ctx(), _write(disguise(CRM_CANARY)))

    assert isinstance(verdict, Deny), disguise(CRM_CANARY)
    assert verdict.code is DenyCode.CANARY_HIT


async def test_ordinary_text_and_look_alikes_pass() -> None:
    layer, recorder = _layer()
    for text in ("Damaged order, please call back", "CNRY-HBL-00000000", "code 3F9A71C2", ""):
        assert isinstance(await layer.before_call(ctx(), _write(text)), Allow)
    assert recorder.events == []


async def test_a_canary_in_a_reads_arguments_is_caught_too() -> None:
    layer, _ = _layer()

    verdict = await layer.before_call(
        ctx(), call({"query": CRM_CANARY}, name="crm__search_accounts", effect="read")
    )

    assert isinstance(verdict, Deny)


async def test_the_alert_names_the_canary_the_tool_and_the_client_and_never_the_text() -> None:
    layer, recorder = _layer()
    await layer.before_call(ctx("harborline-ops-bot"), _write(f"pwned {TICKETING_CANARY}"))

    (alert,) = recorder.events
    assert alert.action == "gateway.alert"
    assert alert.payload["kind"] == "canary_hit"
    assert alert.payload["canary"] == "tickets-description"
    assert alert.payload["client_name"] == "harborline-ops-bot"
    assert TICKETING_CANARY not in str(alert.payload)
    assert "pwned" not in str(alert.payload)


async def test_the_modes() -> None:
    hit = _write(CRM_CANARY)
    seen = {}
    for mode in ("enforce", "monitor", "off"):
        events = MemoryEventSink()
        outcome = await run(pipeline_with(CanaryLayer, mode, events, canaries=CONFIG), hit)
        seen[mode] = (outcome, last_layer(events))

    assert isinstance(seen["enforce"][0], Blocked)
    assert seen["enforce"][1]["code"] == "canary_hit"
    assert not isinstance(seen["monitor"][0], Blocked)
    assert seen["monitor"][1]["verdict"] == "would_block"
    assert seen["monitor"][1]["score"] == 1
    assert seen["off"][1]["verdict"] == "off"


def test_a_mistake_in_the_file_stops_startup(tmp_path: Path) -> None:
    for text in (
        'shape = "("\n',
        "[canaries]\nx = 1\n",
        'shape = "A"\n[canaries]\nx = "short"\n',
        'shape = "A"\nstray = 1\n',
        "not toml [",
    ):
        path = tmp_path / "c.toml"
        path.write_text(text)
        with pytest.raises(CanaryConfigError):
            load_canary_config(path)
    with pytest.raises(CanaryConfigError):
        load_canary_config(tmp_path / "missing.toml")


async def test_filler_words_before_an_encoded_canary_do_not_use_up_the_decoding_budget() -> None:
    layer, _ = _layer()
    filler = " ".join(f"fillerword{n:04d}abcdefgh" for n in range(200))
    encoded = base64.b64encode(f"code: {CRM_CANARY}".encode()).decode()

    verdict = await layer.before_call(ctx(), _write(f"{filler} {encoded}"))

    assert isinstance(verdict, Deny)


async def test_arguments_too_large_to_scan_are_refused_not_scanned_in_part() -> None:
    from ai_gateway.pipeline.layers.canary import _MAX_TEXT

    layer, _ = _layer()

    verdict = await layer.before_call(ctx(), _write("x" * (_MAX_TEXT + 10) + CRM_CANARY))

    assert isinstance(verdict, Deny), "a canary hidden behind padding"
