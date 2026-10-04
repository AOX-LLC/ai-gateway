"""The demo seed: the same seed gives the same rows, the rows fit the telemetry tables' checks,
the demo pipeline is the one the seed says it is, and the Compose override changes only its own."""

import importlib.util
import re
import tomllib
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

from ai_gateway.pipeline.config import parse_pipeline_config
from ai_gateway.pipeline.registry import LAYER_ORDER

ROOT = Path(__file__).resolve().parents[2]


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "seed_dashboard_demo", ROOT / "scripts" / "seed_dashboard_demo.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


seed = _load()
ANCHOR = datetime(2026, 10, 3, 21, 57, tzinfo=UTC)


@pytest.fixture(scope="module")
def week() -> Any:
    return seed.build_backfill(20261003, ANCHOR)


def test_the_same_seed_and_anchor_give_the_same_rows_and_another_seed_does_not(week: Any) -> None:
    again = seed.build_backfill(20261003, ANCHOR)
    other = seed.build_backfill(7, ANCHOR)

    assert week == again
    assert seed.summary(week) == seed.summary(again)
    assert week != other
    assert seed.summary(week)["tool_calls"] != seed.summary(other)["tool_calls"]


def test_the_counts_and_shapes_do_not_depend_on_the_day_the_seed_is_run(week: Any) -> None:
    # The anchor is the newest 21:57 UTC: another day is the same week moved, so the same numbers.
    for now in (
        datetime(2026, 10, 3, 22, 30, tzinfo=UTC),
        datetime(2026, 10, 9, 3, 5, tzinfo=UTC),  # a different weekday, before that day's 21:57
        datetime(2026, 11, 17, 21, 57, tzinfo=UTC),
    ):
        moved = seed.build_backfill(20261003, seed.default_anchor(now))
        assert seed.summary(moved) == seed.summary(week)
        assert seed.default_anchor(now) <= now
        assert now - seed.default_anchor(now) < timedelta(days=1)


def test_the_week_has_a_daily_rhythm_and_two_quiet_days(week: Any) -> None:
    by_hour: Counter[int] = Counter()
    by_day_kind: Counter[str] = Counter()
    for row in week.requests:
        if row["kind"] != "tool_call":
            continue
        by_hour[row["ts"].hour] += 1
        back = (ANCHOR.date() - row["ts"].date()).days
        by_day_kind["quiet" if back in seed.QUIET_DAYS_BACK else "busy"] += 1

    night = sum(by_hour[h] for h in (0, 1, 2, 3, 4, 22, 23)) / 7
    afternoon = sum(by_hour[h] for h in (13, 14, 15, 16)) / 4
    assert afternoon > 3 * night
    quiet_days = len(seed.QUIET_DAYS_BACK)
    busy_days = 8 - quiet_days  # the week spans the anchor's day and the seven before it
    assert by_day_kind["quiet"] / quiet_days < 0.6 * by_day_kind["busy"] / busy_days


def test_the_four_episodes_are_in_the_data(week: Any) -> None:
    episodes = seed.episodes(ANCHOR)

    def inside(row: dict[str, Any], name: str) -> bool:
        return bool(seed._within(row["ts"], episodes[name]))

    probing = [r for r in week.requests if r["client_name"] == seed.DECOY and inside(r, "probing")]
    assert len(probing) > 150
    assert all(r["blocked_by"] == "scope" for r in probing)
    sprayed = [a for a in week.auth_failures if inside(a, "spray")]
    assert len(sprayed) > 150
    burst = [v for v in week.verdicts if v["verdict"] == "would_block"]
    assert len(burst) > 100
    assert all(v["layer"] == "rate_limit" and v["mode"] == "monitor" for v in burst)
    slow = [
        r
        for r in week.requests
        if r["kind"] == "tool_call"
        and inside(r, "slow")
        and r["upstream_status"] == "ok"
        and r["namespace"] == "tickets"
    ]
    normal = [
        r
        for r in week.requests
        if r["kind"] == "tool_call"
        and not inside(r, "slow")
        and r["upstream_status"] == "ok"
        and r["namespace"] == "tickets"
        and r["effect"] == "read"
    ]
    assert sum(r["duration_ms"] for r in slow) / len(slow) > 5 * sum(
        r["duration_ms"] for r in normal
    ) / len(normal)


def test_every_layer_that_blocks_blocks_something_and_the_rows_are_consistent(week: Any) -> None:
    blocked = Counter(r["blocked_by"] for r in week.requests if r["outcome"] == "blocked")

    assert set(blocked) == {"scope", "allowlist", "approval"}
    for row in week.requests:
        if row["outcome"] == "blocked":
            assert row["blocked_by"]
            assert row["deny_code"]
        if row["deny_code"] == "approval_pending":
            assert row["duration_ms"] >= seed.HOLD_MS
    by_request: dict[str, list[int]] = {}
    for verdict in week.verdicts:
        by_request.setdefault(verdict["request_id"], []).append(verdict["ordinal"])
    assert all(ordinals == list(range(len(ordinals))) for ordinals in by_request.values())
    known = {r["request_id"] for r in week.requests}
    assert set(by_request) <= known
    assert len(known) == len(week.requests), "request ids are unique"


def test_the_rows_fit_the_telemetry_tables_checks_and_leave_out_what_it_must_never_hold(
    week: Any,
) -> None:
    name = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
    code = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
    start = ANCHOR - timedelta(days=7)
    for row in week.requests:
        assert start <= row["ts"] <= ANCHOR + timedelta(minutes=1)
        assert row["kind"] in {"tool_call", "tools_list"}
        assert row["outcome"] in {"forwarded", "blocked", "listed"}
        assert row["duration_ms"] >= 0
        assert len(row["client_name"]) <= 100
        if row["tool"]:
            assert len(row["tool"]) <= 64
            assert name.match(row["namespace"])
        for key in ("blocked_by", "deny_code"):
            assert row[key] is None or code.match(row[key])
        assert row["args_sha256"] is None or re.fullmatch(r"[0-9a-f]{64}", row["args_sha256"])
    for failure in week.auth_failures:
        assert re.fullmatch(r"[a-z][a-z0-9_]{0,31}", failure["reason"])
        assert failure["lookup_id"] is None or re.fullmatch(r"[a-z2-7]{8}", failure["lookup_id"])
    # No column for an argument, a result, a credential or an address: only these keys exist.
    assert set(week.requests[0]) == set(seed.REQUEST_COLUMNS)


def test_the_seeds_pipeline_fingerprint_is_the_gateways_for_the_demo_pipeline() -> None:
    demo = tomllib.loads((ROOT / "config" / "pipeline.demo.toml").read_text(encoding="utf-8"))
    parsed = parse_pipeline_config(demo, LAYER_ORDER)

    assert parsed.sha256 == seed.config_fingerprint(seed.LAYERS)
    assert [(name, mode.value) for name, mode in parsed.modes.items()] == seed.LAYERS
    assert parsed.modes["rate_limit"].value == "monitor"


def test_the_demo_override_changes_only_what_it_says_and_never_the_real_admin_credential() -> None:
    override = yaml.safe_load((ROOT / "compose.demo.yaml").read_text(encoding="utf-8"))

    assert set(override) == {"name", "services"}
    assert override["name"] == "ai-gateway-demo", "by hand it must still not be the real project"
    assert set(override["services"]) == {"gateway", "dashboard"}
    assert set(override["services"]["gateway"]) == {"environment"}
    assert set(override["services"]["dashboard"]) == {"environment"}
    gateway = override["services"]["gateway"]["environment"]
    assert gateway == {
        "GATEWAY_APPROVAL_TTL_S": "86400",
        "GATEWAY_APPROVAL_HOLD_S": "3",
        "GATEWAY_PIPELINE_FILE": "/app/config/pipeline.demo.toml",
    }
    dashboard = override["services"]["dashboard"]["environment"]
    assert dashboard["DASHBOARD_DEMO"] == "1"
    assert "DEMO_DASHBOARD_PASSWORD_HASH" in dashboard["DASHBOARD_ADMIN_PASSWORD_HASH"]
    assert ":?" in dashboard["DASHBOARD_ADMIN_PASSWORD_HASH"], (
        "required, with no default to fall back to"
    )
    secret = dashboard["DASHBOARD_SESSION_SECRET"]
    assert "DEMO_SESSION_SECRET" in secret, "its own secret, not .env's"
    assert ":?" in secret, "required, so a cookie signed with the real secret is never accepted"


def _docker_says(monkeypatch: pytest.MonkeyPatch, module: ModuleType, stdout: str) -> None:
    def fake_run(*_: Any, **__: Any) -> Any:
        return type("Done", (), {"stdout": stdout})()

    monkeypatch.setattr(module.subprocess, "run", fake_run)


def test_the_seed_refuses_a_database_that_is_not_the_demo_stacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load()

    def says(stdout: str) -> str | None:
        _docker_says(monkeypatch, module, stdout)
        refusal: str | None = module.not_the_demo_stack(4402)
        return refusal

    assert "not the demo stack's" in (says("ai-gateway\n") or "")
    assert "none found" in (says("") or "")
    assert says("ai-gateway-demo\n") is None


def test_the_seed_refuses_when_it_cannot_tell_whose_database_it_is(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load()

    def no_docker(*_: Any, **__: Any) -> Any:
        raise FileNotFoundError("docker")

    monkeypatch.setattr(module.subprocess, "run", no_docker)

    assert "cannot tell" in (module.not_the_demo_stack(4402) or "")


def test_the_demo_model_usage_is_all_replayed_priced_like_the_recordings_and_agrees_with_verdicts(
    week: Any,
) -> None:
    usage = week.usage
    assert usage
    assert {u["mode"] for u in usage} == {"replay"}  # nothing the demo shows was billed
    for u in usage[:500]:
        cost = (u["input_tokens"] * 1.0 + u["output_tokens"] * 5.0) / 1e6
        assert u["cost_usd"] == pytest.approx(cost, abs=1e-8)
    unrecorded = [u for u in usage if u["status"] == "unrecorded"]
    assert unrecorded
    assert all(
        u["input_tokens"] == u["output_tokens"] == 0 and u["cost_usd"] == 0 for u in unrecorded
    )
    unclassified = {v["request_id"] for v in week.verdicts if v["verdict"] == "unclassified"}
    assert {u["request_id"] for u in unrecorded} == unclassified
    assert {v["code"] for v in week.verdicts if v["verdict"] == "unclassified"} == {
        "classifier_unrecorded"
    }
