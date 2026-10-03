"""gateway-admin seed-demo and tool-policy-set, run the way the Compose admin service runs them."""

import json
from pathlib import Path

import anyio
import psycopg
import pytest

from ai_gateway.admin import cli
from ai_gateway.registry.repo import AdminRegistry

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

REPO_ROOT = Path(__file__).resolve().parents[2]


async def _admin(monkeypatch: pytest.MonkeyPatch, database_url: str, *argv: str) -> None:
    monkeypatch.setenv("GATEWAY_MIGRATE_DATABASE_URL", database_url)
    await anyio.to_thread.run_sync(cli.main, list(argv))


async def _rows(url: str, query: str) -> list[tuple[object, ...]]:
    async with await psycopg.AsyncConnection.connect(url) as connection:
        cursor = await connection.execute(query.encode())
        return await cursor.fetchall()


async def _seed_demo(
    monkeypatch: pytest.MonkeyPatch, url: str, capsys: pytest.CaptureFixture[str]
) -> dict[str, str]:
    await _admin(
        monkeypatch,
        url,
        "seed-demo",
        "--policies",
        str(REPO_ROOT / "config" / "tool_policies.toml"),
    )
    printed: dict[str, str] = json.loads(capsys.readouterr().out)
    return printed


async def test_seed_demo_registers_the_upstream_policies_and_clients(
    monkeypatch: pytest.MonkeyPatch,
    test_database_url: str,
    clean_database: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tokens = await _seed_demo(monkeypatch, test_database_url, capsys)

    upstream = await _rows(
        test_database_url,
        "SELECT namespace, url, credential_env FROM upstream_servers ORDER BY namespace",
    )
    policies = await _rows(
        test_database_url, "SELECT effect, count(*) FROM tool_policies GROUP BY effect ORDER BY 1"
    )
    scopes = await _rows(
        test_database_url,
        "SELECT c.name, count(*) FROM clients c JOIN client_scopes s ON s.client_id = c.id"
        " GROUP BY c.name ORDER BY 1",
    )
    assert sorted(tokens) == [
        "harborline-decoy-bot",
        "harborline-ops-bot",
        "harborline-support-bot",
    ]
    assert upstream == [
        ("crm", "http://crm:4411/mcp", "CRM_SERVICE_TOKEN"),
        ("handbook", "http://handbook:4410/mcp", "HANDBOOK_SERVICE_TOKEN"),
        ("tickets", "http://ticketing:4412/mcp", "TICKETING_SERVICE_TOKEN"),
    ]
    assert policies == [("read", 7), ("write", 4)]
    assert scopes == [("harborline-ops-bot", 11), ("harborline-support-bot", 9)]


async def test_seed_demo_is_idempotent_and_replaces_the_old_tokens(
    monkeypatch: pytest.MonkeyPatch,
    test_database_url: str,
    clean_database: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    first = await _seed_demo(monkeypatch, test_database_url, capsys)
    second = await _seed_demo(monkeypatch, test_database_url, capsys)

    live = await _rows(
        test_database_url,
        "SELECT count(*) FROM client_tokens WHERE revoked_at IS NULL",
    )
    counts = await _rows(
        test_database_url,
        "SELECT (SELECT count(*) FROM upstream_servers), (SELECT count(*) FROM tool_policies),"
        " (SELECT count(*) FROM clients)",
    )
    assert first != second
    assert live == [(3,)]
    assert counts == [(3, 11, 3)]


async def test_tool_policy_set_changes_one_tools_effect(
    monkeypatch: pytest.MonkeyPatch,
    test_database_url: str,
    clean_database: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    await _admin(monkeypatch, test_database_url, "tool-policy-set", "crm__find", "read")
    await _admin(monkeypatch, test_database_url, "tool-policy-set", "crm__find", "write")

    rows = await _rows(test_database_url, "SELECT namespace, tool, effect FROM tool_policies")
    assert rows == [("crm", "find", "write")]


async def test_tool_policy_set_rejects_a_name_without_a_namespace(
    monkeypatch: pytest.MonkeyPatch, test_database_url: str, clean_database: None
) -> None:
    with pytest.raises(SystemExit, match="not a <namespace>__<tool> name"):
        await _admin(monkeypatch, test_database_url, "tool-policy-set", "find", "read")


async def _scopes_by_client(url: str) -> dict[str, list[str]]:
    rows = await _rows(
        url,
        "SELECT c.name, array_agg(s.tool ORDER BY s.tool) FROM clients c"
        " JOIN client_scopes s ON s.client_id = c.id GROUP BY c.name",
    )
    return {str(name): list(tools) for name, tools in rows}  # type: ignore[call-overload]


@pytest.mark.parametrize("order", [("seed-test", "seed-demo"), ("seed-demo", "seed-test")])
async def test_seeding_both_data_sets_leaves_each_client_with_exactly_its_own_scopes(
    monkeypatch: pytest.MonkeyPatch,
    test_database_url: str,
    clean_database: None,
    capsys: pytest.CaptureFixture[str],
    order: tuple[str, str],
) -> None:
    policies = str(REPO_ROOT / "config" / "tool_policies.toml")
    for command in order:
        extra = ["--policies", policies] if command == "seed-demo" else []
        await _admin(monkeypatch, test_database_url, command, *extra)
    capsys.readouterr()

    scopes = await _scopes_by_client(test_database_url)
    assert scopes["echo-test-narrow"] == ["echo__say"]
    assert scopes["echo-test-wide"] == ["echo__say", "echo__shout"]
    assert len(scopes["harborline-support-bot"]) == 9
    assert len(scopes["harborline-ops-bot"]) == 11
    assert not any(tool.startswith("echo__") for tool in scopes["harborline-ops-bot"])
    assert sorted(scopes) == [
        "echo-test-narrow",
        "echo-test-wide",
        "harborline-ops-bot",
        "harborline-support-bot",
    ]


async def test_seeding_again_removes_scopes_that_are_no_longer_listed(
    monkeypatch: pytest.MonkeyPatch,
    test_database_url: str,
    clean_database: None,
    admin_registry: AdminRegistry,
) -> None:
    await _admin(monkeypatch, test_database_url, "seed-test")
    client_id = await admin_registry.client_id("echo-test-narrow")
    await admin_registry.grant_scopes(client_id, ["echo__shout", "tickets__assign"])

    await _admin(monkeypatch, test_database_url, "seed-test")

    assert (await _scopes_by_client(test_database_url))["echo-test-narrow"] == ["echo__say"]


async def test_seed_demo_removes_policies_that_are_no_longer_in_the_file(
    monkeypatch: pytest.MonkeyPatch,
    test_database_url: str,
    clean_database: None,
    admin_registry: AdminRegistry,
    tmp_path: Path,
) -> None:
    await admin_registry.replace_tool_policies("tickets", [("get_ticket", "read", "")])
    await admin_registry.replace_tool_policies("other", [("keep_me", "write", "")])
    await admin_registry.upsert_tool_policy("tickets", "retired_tool", "read")
    policies = tmp_path / "policies.toml"
    policies.write_text(
        '[tickets.get_ticket]\neffect = "read"\n\n[tickets.assign]\neffect = "write"\n'
    )

    await _admin(monkeypatch, test_database_url, "seed-demo", "--policies", str(policies))

    rows = await _rows(
        test_database_url, "SELECT namespace, tool, effect FROM tool_policies ORDER BY 1, 2"
    )
    assert rows == [
        ("other", "keep_me", "write"),
        ("tickets", "assign", "write"),
        ("tickets", "get_ticket", "read"),
    ]


async def test_replacing_policies_is_all_or_nothing(
    test_database_url: str, clean_database: None, admin_registry: AdminRegistry
) -> None:
    await admin_registry.replace_tool_policies("tickets", [("get_ticket", "read", "")])

    with pytest.raises(psycopg.errors.CheckViolation):
        await admin_registry.replace_tool_policies(
            "tickets", [("assign", "write", ""), ("bad name!", "read", "")]
        )

    rows = await _rows(test_database_url, "SELECT tool FROM tool_policies")
    assert rows == [("get_ticket",)]


SUPPORT_TOOLS = [
    "crm__get_account",
    "crm__list_deals",
    "crm__search_accounts",
    "handbook__get_document",
    "handbook__search",
    "tickets__add_comment",
    "tickets__create_ticket",
    "tickets__get_ticket",
    "tickets__list_tickets",
]


async def test_the_demo_clients_have_exactly_the_agreed_scopes(
    monkeypatch: pytest.MonkeyPatch,
    test_database_url: str,
    clean_database: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    await _seed_demo(monkeypatch, test_database_url, capsys)

    scopes = await _scopes_by_client(test_database_url)
    assert scopes["harborline-support-bot"] == SUPPORT_TOOLS
    assert scopes["harborline-ops-bot"] == sorted(
        [*SUPPORT_TOOLS, "tickets__assign", "tickets__change_status"]
    )
    # The support bot can read the CRM and the handbook, and change nothing there or in
    # tickets beyond opening one and adding a comment.
    assert "tickets__assign" not in scopes["harborline-support-bot"]
    assert "tickets__change_status" not in scopes["harborline-support-bot"]


async def test_every_demo_tool_has_a_reviewed_policy_and_the_new_ones_only_read(
    monkeypatch: pytest.MonkeyPatch,
    test_database_url: str,
    clean_database: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    await _seed_demo(monkeypatch, test_database_url, capsys)

    rows = await _rows(
        test_database_url, "SELECT namespace || '__' || tool, effect FROM tool_policies"
    )
    effects = {str(name): str(effect) for name, effect in rows}
    scopes = await _scopes_by_client(test_database_url)
    assert set(scopes["harborline-ops-bot"]) == set(effects)
    for name, effect in effects.items():
        if name.startswith(("crm__", "handbook__")):
            assert effect == "read", name


async def test_seed_test_classifies_the_echo_tools_as_reads_so_none_needs_an_approver_role(
    monkeypatch: pytest.MonkeyPatch,
    test_database_url: str,
    clean_database: None,
) -> None:
    """Unclassified they would be writes (the fail-closed default), and with the approval layer
    enforcing, a write with no approver role is refused: the acceptance check would never pass."""
    await _admin(monkeypatch, test_database_url, "seed-test")

    async with await psycopg.AsyncConnection.connect(test_database_url) as connection:
        cursor = await connection.execute(
            "SELECT namespace, tool, effect FROM tool_policies WHERE namespace = 'echo'"
        )
        assert sorted(await cursor.fetchall()) == [
            ("echo", "say", "read"),
            ("echo", "shout", "read"),
            ("echo", "wait", "read"),
        ]
