"""What must stay true of how approvals are configured and who can reach the test approver."""

import argparse
import ast
import importlib.util
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

from ai_gateway.admin.cli import _approver_add
from ai_gateway.approver.cli import Approvals
from ai_gateway.policy.approvals import expire_due_forever
from ai_gateway.policy.roles import ApprovalRolesError, load_roles_by_action
from ai_gateway.seams.approvals import ApprovalOutcome
from tests.test_approval_gate import HUMAN, ROLES, _approver, _call, _context, _gate

ROOT = Path(__file__).resolve().parents[2]
SOURCES = [*(ROOT / "gateway" / "src").rglob("*.py"), *(ROOT / "scripts").rglob("*.py")]


def _keyword_uses(name: str) -> list[str]:
    found = []
    for path in SOURCES:
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.keyword) and node.arg == name:
                found.append(f"{path.relative_to(ROOT)}:{node.value.lineno}")
            if isinstance(node, ast.Name | ast.Attribute) and getattr(node, "attr", "") == name:
                found.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    return found


# --- the approver's rules come from a list the requester cannot choose from -----------------


def test_trust_requester_role_is_never_set_outside_tests() -> None:
    assert _keyword_uses("trust_requester_role") == []
    assert "trust_requester_role" not in "".join(p.read_text() for p in SOURCES)


def test_the_gateway_never_names_delegates_so_only_the_asking_client_can_use_an_approval() -> None:
    assert _keyword_uses("delegates") == []


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
        test_database_url: str,
    ) -> None:
        await _approver_add(
            test_database_url, argparse.Namespace(id="aiden", name="Aiden", role=None)
        )
        pending = await _gate(policy_gateway_url).decide(_context(), _call())
        elsewhere = Approvals(policy_approver_url, {"tickets__assign": "approver"})

        with pytest.raises(NotAuthorizedToResolveError):
            await elsewhere.decide(UUID(pending.approval_id or ""), "aiden", Decision.APPROVE, None)

    async def test_a_request_with_another_role_than_the_list_names_is_refused(
        self,
        policy: None,
        policy_gateway_url: str,
        policy_approver_url: str,
        test_database_url: str,
    ) -> None:
        """The requester writes required_role; the approver's own list decides what it must be."""
        await _approver_add(
            test_database_url, argparse.Namespace(id="aiden", name="Aiden", role=None)
        )
        gate = _gate(policy_gateway_url, roles_by_action={"tickets__change_status": "anyone"})
        pending = await gate.decide(_context(), _call())

        with pytest.raises(NotAuthorizedToResolveError):
            await Approvals(policy_approver_url, ROLES).decide(
                UUID(pending.approval_id or ""), "aiden", Decision.APPROVE, None
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

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(expire_due_forever, gate, 0.2)
            with anyio.fail_after(5):
                while True:
                    stored = await _approver(policy_approver_url).get(UUID(first.approval_id or ""))
                    if stored.status.value == "expired":
                        break
                    await anyio.sleep(0.2)
            tasks.cancel_scope.cancel()


# --- the test approver cannot be reached from the default stack -----------------------------


def _compose() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load((ROOT / "compose.yaml").read_text())
    return loaded


def test_no_compose_service_runs_or_enables_the_test_approver() -> None:
    text = (ROOT / "compose.yaml").read_text()

    assert "LAB_AUTO_APPROVE" not in text
    assert "auto_approver" not in text
    assert "--approve-as" not in text


def test_the_gateway_service_holds_no_approver_credential() -> None:
    services = _compose()["services"]

    holders = [
        name
        for name, service in services.items()
        if "POLICY_APPROVER_DATABASE_URL" in str(service.get("environment", {}))
    ]
    assert holders == ["approver"], "only the person's tool, which is behind the tools profile"
    assert services["approver"]["profiles"] == ["tools"]
    assert "POLICY_APPROVER" not in str(services["gateway"].get("environment", {}))


def test_the_images_do_not_contain_the_scripts() -> None:
    for dockerfile in (ROOT / "gateway" / "Dockerfile", ROOT / "servers" / "Dockerfile"):
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


def test_the_default_environment_and_the_workflow_do_not_switch_it_on() -> None:
    assert "LAB_AUTO_APPROVE" not in (ROOT / ".env.example").read_text()
    assert "LAB_AUTO_APPROVE" not in (ROOT / ".github" / "workflows" / "ci.yml").read_text()


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
