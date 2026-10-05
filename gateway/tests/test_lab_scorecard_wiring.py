"""What the scorecard needs from Compose and the lab approver, without changing a default.

Every variable added to the gateway service has a default equal to what was hard-coded before, so
the default stack is byte-for-byte the same; the lab approver gains a decision mode that is
`approve` unless the run says `reject`. Needs no database. Harborline Supply Co. is fictional."""

import sys
from pathlib import Path

import pytest
import yaml
from aox_agent_core.approvals import Decision

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import lab_approver  # noqa: E402

COMPOSE = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
GATEWAY = COMPOSE["services"]["gateway"]["environment"]
LAB_APPROVER = COMPOSE["services"]["lab-approver"]["environment"]


@pytest.mark.parametrize(
    ("variable", "default"),
    [
        ("GATEWAY_PIPELINE_FILE", "/app/config/pipeline.toml"),
        ("GATEWAY_TOOL_PINS_FILE", "/app/config/tool_pins.toml"),
        ("GATEWAY_APPROVAL_ROLES_FILE", "/app/config/approval_roles.toml"),
        ("GATEWAY_ALLOWLIST_FILE", "/app/config/allowlist.toml"),
        ("GATEWAY_CLASSIFIER_FILE", "/app/config/classifier.toml"),
    ],
)
def test_each_gateway_config_file_can_be_pointed_elsewhere_and_defaults_to_the_products(
    variable: str, default: str
) -> None:
    value = GATEWAY[variable]

    assert value.startswith("${")
    assert value.endswith("}")
    assert value.endswith(f":-{default}}}"), value
    assert "_IN_CONTAINER" in value


def test_the_catalogue_refresh_defaults_to_the_gateways_own_default() -> None:
    from ai_gateway.settings import GatewaySettings

    assert GATEWAY["GATEWAY_CATALOG_REFRESH_S"] == "${GATEWAY_CATALOG_REFRESH_S:-60}"
    assert GatewaySettings.model_fields["catalog_refresh_s"].default == 60.0


def test_the_lab_upstreams_credential_reaches_the_gateway_only_when_a_run_gives_it() -> None:
    assert GATEWAY["LAB_UPSTREAM_SERVICE_TOKEN"] == "${LAB_UPSTREAM_TOKEN:-}"  # noqa: S105


def test_every_file_variable_the_gateway_service_sets_is_a_setting_it_reads() -> None:
    from ai_gateway.settings import GatewaySettings

    fields = set(GatewaySettings.model_fields)
    for name in GATEWAY:
        if name.startswith("GATEWAY_"):
            assert name.removeprefix("GATEWAY_").lower() in fields, f"{name} is never read"


def test_the_lab_approver_approves_unless_a_run_says_reject() -> None:
    assert LAB_APPROVER["LAB_APPROVER_DECISION"] == "${LAB_APPROVER_DECISION:-approve}"
    assert lab_approver.decision_from_env(None) is Decision.APPROVE
    assert lab_approver.decision_from_env("") is Decision.APPROVE
    assert lab_approver.decision_from_env("approve") is Decision.APPROVE
    assert lab_approver.decision_from_env("reject") is Decision.REJECT


@pytest.mark.parametrize("value", ["Reject", "deny", "yes", "approve ", "REJECT"])
def test_any_other_decision_stops_the_lab_approver(value: str) -> None:
    with pytest.raises(SystemExit):
        lab_approver.decision_from_env(value)


def test_the_lab_approver_says_when_it_is_ready_and_the_runner_waits_for_that_line() -> None:
    """Writes made before the approver is connected get a pending result and are not approved: the
    first column of an early scorecard run lost four benign writes to exactly that. The approver
    prints a line once it has checked its login; the runner waits for it."""
    from redteam.stack import APPROVER_READY

    source = (ROOT / "scripts" / "auto_approver.py").read_text(encoding="utf-8")

    assert APPROVER_READY in source
    assert source.index(APPROVER_READY) > source.index("principal.id != f"), "after the login check"


def test_changing_the_approver_never_re_runs_the_policy_setup_it_depends_on() -> None:
    """`up --force-recreate lab-approver` alone also recreates policy-setup, which resets the lab
    role's grants while attacks are running. Every compose call that names the lab approver in the
    runner must pass --no-deps."""
    import re

    text = (ROOT / "scripts" / "redteam" / "scorecard_run.py").read_text(encoding="utf-8")
    calls = re.findall(r"compose\((.*?)\)\s*$", text, flags=re.S | re.M)
    naming = [call for call in calls if '"lab-approver"' in call]

    assert naming, "the runner no longer starts the approver: update this test"
    assert all('"--no-deps"' in call for call in naming)
