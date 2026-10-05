"""What must stay true of how approvals are configured and who can reach the test approver."""

import ast
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any
from uuid import UUID

import anyio
import pytest
import yaml
from aox_agent_core.approvals import Decision, Principal, PrincipalKind
from aox_agent_core.errors import NotAuthorizedToResolveError

from ai_gateway.approver.cli import Approvals
from ai_gateway.policy.approvals import expire_due_forever
from ai_gateway.policy.roles import ApprovalRolesError, load_roles_by_action
from ai_gateway.seams.approvals import ApprovalOutcome
from tests.test_approval_gate import HUMAN, ROLES, _approver, _call, _context, _gate

ROOT = Path(__file__).resolve().parents[2]
SOURCES = [*(ROOT / "gateway" / "src").rglob("*.py"), *(ROOT / "scripts").rglob("*.py")]


def _keyword_uses(name: str, *, attributes: bool = True) -> list[str]:
    found = []
    for path in SOURCES:
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.keyword) and node.arg == name:
                found.append(f"{path.relative_to(ROOT)}:{node.value.lineno}")
            if (
                attributes
                and isinstance(node, ast.Name | ast.Attribute)
                and getattr(node, "attr", "") == name
            ):
                found.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    return found


# --- the approver's rules come from a list the requester cannot choose from -----------------


def test_trust_requester_role_is_never_set_outside_tests() -> None:
    assert _keyword_uses("trust_requester_role") == []
    assert "trust_requester_role" not in "".join(p.read_text() for p in SOURCES)


def test_the_gateway_never_names_delegates_so_only_the_asking_client_can_use_an_approval() -> None:
    # Passed to nothing: reading `request.delegates` (to refuse a request that has some) is fine.
    assert _keyword_uses("delegates", attributes=False) == []


def test_every_write_tool_has_an_approver_role_and_only_write_tools_do() -> None:
    policies = tomllib.loads((ROOT / "config" / "tool_policies.toml").read_text())
    writes = {
        f"{namespace}__{tool}"
        for namespace, tools in policies.items()
        for tool, policy in tools.items()
        if policy["effect"] == "write"
    }
    roles = load_roles_by_action(ROOT / "config" / "approval_roles.toml")

    assert writes
    assert set(roles) == writes
    assert set(roles.values()) == {"approver"}


@pytest.mark.parametrize(
    "text",
    ["roles_by_action = 1", "[roles_by_action]\nx = 1", "[other]\na = 'b'", "not toml ["],
)
def test_a_mistake_in_the_roles_file_stops_startup(tmp_path: Path, text: str) -> None:
    path = tmp_path / "roles.toml"
    path.write_text(text)

    with pytest.raises(ApprovalRolesError):
        load_roles_by_action(path)


@pytest.mark.integration
@pytest.mark.anyio
class TestWithTheDatabase:
    async def test_a_write_with_no_listed_role_is_not_asked_for_and_not_run(
        self, policy: None, policy_gateway_url: str, policy_approver_url: str
    ) -> None:
        gate = _gate(policy_gateway_url, roles_by_action={})

        decision = await gate.decide(_context(), _call())

        assert decision.outcome is ApprovalOutcome.UNAVAILABLE
        assert await _approver(policy_approver_url).list_pending(HUMAN) == []

    async def test_the_request_names_the_listed_role_and_no_delegates(
        self, policy: None, policy_gateway_url: str, policy_approver_url: str
    ) -> None:
        pending = await _gate(policy_gateway_url).decide(_context(), _call())

        request = await _approver(policy_approver_url).get(UUID(pending.approval_id or ""))

        assert request.required_role == "approver"
        assert request.delegates == frozenset()

    async def test_an_approver_cannot_decide_an_action_the_list_does_not_name(
        self,
        policy: None,
        policy_gateway_url: str,
        policy_approver_url: str,
    ) -> None:
        pending = await _gate(policy_gateway_url).decide(_context(), _call())
        elsewhere = Approvals(policy_approver_url, {"tickets__assign": "approver"})

        with pytest.raises(NotAuthorizedToResolveError):
            await elsewhere.decide(UUID(pending.approval_id or ""), Decision.APPROVE, None)

    async def test_a_request_with_another_role_than_the_list_names_is_refused(
        self,
        policy: None,
        policy_gateway_url: str,
        policy_approver_url: str,
    ) -> None:
        """The requester writes required_role; the approver's own list decides what it must be."""
        gate = _gate(policy_gateway_url, roles_by_action={"tickets__change_status": "anyone"})
        pending = await gate.decide(_context(), _call())

        with pytest.raises(NotAuthorizedToResolveError):
            await Approvals(policy_approver_url, ROLES).decide(
                UUID(pending.approval_id or ""), Decision.APPROVE, None
            )

    async def test_requests_past_their_lifetime_are_stored_as_expired_and_asked_for_afresh(
        self, policy: None, policy_gateway_url: str, policy_approver_url: str
    ) -> None:
        gate, ctx, call = _gate(policy_gateway_url, ttl_s=1, hold_s=0), _context(), _call()
        first = await gate.decide(ctx, call)
        await anyio.sleep(1.2)

        expired = await gate.expire_due(Principal(id="service:gateway", kind=PrincipalKind.SERVICE))

        assert expired == 1
        stored = await _approver(policy_approver_url).get(UUID(first.approval_id or ""))
        assert stored.status.value == "expired"
        second = await gate.decide(ctx, call)
        assert second.outcome is ApprovalOutcome.PENDING
        assert second.approval_id != first.approval_id

    async def test_the_periodic_task_expires_requests_on_its_own(
        self, policy: None, policy_gateway_url: str, policy_approver_url: str
    ) -> None:
        gate = _gate(policy_gateway_url, ttl_s=1, hold_s=0)
        first = await gate.decide(_context(), _call())
        approver = _approver(policy_approver_url)  # one connection pool, not one per look

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(expire_due_forever, gate, 0.2)
            with anyio.fail_after(5):
                while True:
                    stored = await approver.get(UUID(first.approval_id or ""))
                    if stored.status.value == "expired":
                        break
                    await anyio.sleep(0.2)
            tasks.cancel_scope.cancel()


# --- the test approver cannot be reached from the default stack -----------------------------


def _compose() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load((ROOT / "compose.yaml").read_text())
    return loaded


def test_the_test_approver_exists_in_compose_only_as_the_lab_service_behind_both_switches() -> None:
    """Off in the default stack: it is the `lab` profile's one service, which holds no default for
    the switch (an empty one: the service refuses to run) and is not restarted. Nothing else in
    Compose names the switch or the script."""
    services = _compose()["services"]

    mentions = [
        name
        for name, service in services.items()
        if "LAB_AUTO_APPROVE" in str(service) or "auto_approver" in str(service)
    ]
    assert mentions == ["lab-approver"]
    lab = services["lab-approver"]
    assert lab["profiles"] == ["lab"], "not started by `docker compose up`"
    assert lab["environment"]["LAB_AUTO_APPROVE"] == "${LAB_AUTO_APPROVE:-}", "no default: empty"
    assert "policy_lab_approver:${POLICY_LAB_APPROVER_DB_PASSWORD:-}@" in str(
        lab["environment"]["POLICY_APPROVER_DATABASE_URL"]
    ), "the lab role's password, empty unless set"
    assert lab["restart"] == "no"
    assert lab["volumes"] == [
        "./scripts/auto_approver.py:/lab/auto_approver.py:ro",
        "./scripts/lab_approver.py:/lab/lab_approver.py:ro",
    ]
    # The profile holds the two lab-only services: the approver, and the lab upstream of the
    # scorecard (fenced in test_lab_upstream.py). A third needs its own fence and a line here.
    in_lab = [n for n, s in services.items() if "lab" in s.get("profiles", [])]
    assert in_lab == ["lab-approver", "lab-upstream"]
    assert "--approve-as" not in (ROOT / "compose.yaml").read_text()


def _docker_config(
    tmp_path: Path, *profiles: str, **extra: str
) -> subprocess.CompletedProcess[str]:
    env_file = tmp_path / ".env"
    if not env_file.exists():
        subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "init_env.py"),
                "--example",
                str(ROOT / ".env.example"),
                "--output",
                str(env_file),
            ],
            check=True,
            capture_output=True,
        )
    command = ["docker", "compose", "--env-file", str(env_file)]
    for profile in profiles:
        command += ["--profile", profile]
    return subprocess.run(
        [*command, "config", "-q"],
        cwd=ROOT,
        env={"PATH": os.environ["PATH"], "HOME": str(tmp_path), **extra},
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_default_stack_and_the_lab_profile_both_resolve_without_the_lab_switches(
    tmp_path: Path,
) -> None:
    """Compose interpolates every service's variables whatever the profile: a variable marked
    required on the lab service would break `docker compose` for everyone. None is."""
    if shutil.which("docker") is None:
        pytest.skip("docker is not installed")

    default = _docker_config(tmp_path)
    lab = _docker_config(tmp_path, "lab")

    assert default.returncode == 0, default.stderr
    assert lab.returncode == 0, lab.stderr


def test_the_lab_approver_refuses_to_run_without_its_switch(tmp_path: Path) -> None:
    """The service is the fence: with the lab profile on but LAB_AUTO_APPROVE not set to yes, it
    exits at once, before it reads a database URL or approves anything."""
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "PYTHONPATH": str(ROOT / "scripts")}

    refused = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "lab_approver.py")],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    wrong_value = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "lab_approver.py")],
        env={**env, "LAB_AUTO_APPROVE": "true"},
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    for result in (refused, wrong_value):
        assert result.returncode != 0
        assert "LAB_AUTO_APPROVE=yes" in result.stderr


def test_the_gateway_service_holds_no_approver_credential() -> None:
    services = _compose()["services"]

    holders = sorted(
        name
        for name, service in services.items()
        if "POLICY_APPROVER_DATABASE_URL" in str(service.get("environment", {}))
    )
    assert holders == ["approver", "lab-approver"], "a person's tool, and the lab's: both opt-in"
    assert services["approver"]["profiles"] == ["tools"]
    assert "POLICY_APPROVER" not in str(services["gateway"].get("environment", {}))
    assert "POLICY_LAB" not in str(services["gateway"].get("environment", {}))


def test_the_images_do_not_contain_the_scripts() -> None:
    for dockerfile in (
        ROOT / "gateway" / "Dockerfile",
        ROOT / "servers" / "Dockerfile",
        ROOT / "servers" / "echo" / "Dockerfile",
    ):
        copies = [
            line for line in dockerfile.read_text().splitlines() if line.strip().startswith("COPY")
        ]
        # The servers image carries the two files the model fetch needs and no other script.
        allowed = "COPY scripts/fetch_model.py scripts/potion-base-8M.sha256 /scripts/"
        assert [line for line in copies if "scripts" in line and line != allowed] == [], dockerfile
        assert not [line for line in copies if "auto_approver" in line]


def test_nothing_in_the_gateway_imports_the_test_approver() -> None:
    for path in (ROOT / "gateway" / "src").rglob("*.py"):
        assert "auto_approver" not in path.read_text(), path


def _paths_naming(value: Any, needle: str, path: str = "") -> list[str]:
    """Every key path in parsed YAML whose key or string value contains `needle`."""
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            here = f"{path}.{key}" if path else str(key)
            if needle in str(key):
                found.append(here)
            found += _paths_naming(item, needle, here)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found += _paths_naming(item, needle, f"{path}[{index}]")
    elif needle in str(value):
        found.append(path)
    return found


def test_the_default_environment_and_the_workflow_do_not_switch_it_on_globally() -> None:
    """The switch appears in one place in everything under .github: the `env` of the one named step
    that runs the fictional Harborline stack with the test approver. A job-level env, a container's,
    a `run:` body, a `with:` input, another workflow, or a second step would all add a path."""
    assert "LAB_AUTO_APPROVE" not in (ROOT / ".env.example").read_text()
    paths = []
    for workflow_file in sorted((ROOT / ".github").rglob("*.y*ml")):
        workflow = yaml.safe_load(workflow_file.read_text())
        paths += [
            f"{workflow_file.name}:{path}" for path in _paths_naming(workflow, "LAB_AUTO_APPROVE")
        ]
    steps = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())["jobs"]["e2e"][
        "steps"
    ]
    named = [
        index
        for index, step in enumerate(steps)
        if step.get("name")
        == (
            "Harborline scenario through the gateway and directly, simulated traffic,"
            " then the export attack"
        )
    ]
    assert len(named) == 1
    assert paths == [f"ci.yml:jobs.e2e.steps[{named[0]}].env.LAB_AUTO_APPROVE"]
    assert steps[named[0]]["env"]["LAB_AUTO_APPROVE"] == "yes"


def test_no_script_sets_the_switch_and_nothing_writes_the_workflow_environment() -> None:
    assignment = re.compile(
        r"^\s*(export\s+(\w+\s+)*|env\s+)?LAB_AUTO_APPROVE=|export\s[^#]*LAB_AUTO_APPROVE"
        r"|environ\[[\"']LAB_AUTO_APPROVE[\"']\]\s*="
    )
    for path in [*(ROOT / "scripts").rglob("*"), *(ROOT / ".github").rglob("*")]:
        if not path.is_file() or path.suffix in {".pyc"}:
            continue
        text = path.read_text()
        assert "GITHUB_ENV" not in text, path
        for line in text.splitlines():
            code = line.split("#", 1)[0]
            if path.name == "auto_approver.py":
                continue  # it reads the switch and says so in its messages; it never sets it
            assert not assignment.search(code), (path, line)


def test_direct_check_mounts_only_the_test_client_and_its_scenarios() -> None:
    volumes = [str(v) for v in _compose()["services"]["direct-check"]["volumes"]]

    assert volumes == [
        "./scripts/test_client.py:/scripts/test_client.py:ro",
        "./scripts/scenarios:/scripts/scenarios:ro",
    ]


def _load_auto_approver() -> Any:
    path = ROOT / "scripts" / "auto_approver.py"
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location("auto_approver", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.anyio
async def test_the_test_approver_refuses_to_run_without_both_switches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_auto_approver()
    monkeypatch.delenv("LAB_AUTO_APPROVE", raising=False)
    monkeypatch.setenv("POLICY_APPROVER_DATABASE_URL", "postgresql://x:y@127.0.0.1:1/z")

    async with module.auto_approving(None) as nothing:
        assert nothing.approved == 0, "no approver id: it does nothing"
    with pytest.raises(SystemExit, match="LAB_AUTO_APPROVE"):
        async with module.auto_approving("harborline-approver"):
            pass


def test_the_test_client_that_the_servers_image_runs_does_not_need_agent_core_to_import() -> None:
    """direct-check runs scripts/test_client.py in the servers image, which has no agent-core: the
    test approver is imported only when --approve-as asks for it."""
    tree = ast.parse((ROOT / "scripts" / "test_client.py").read_text())

    top_level = [
        node
        for node in tree.body
        if isinstance(node, ast.Import | ast.ImportFrom)
        and any(
            "auto_approver" in ast.dump(node) or "aox_agent_core" in ast.dump(node) for _ in [0]
        )
    ]
    assert top_level == []
