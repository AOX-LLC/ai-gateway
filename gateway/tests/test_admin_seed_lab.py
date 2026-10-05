"""gateway-admin seed-lab: the scorecard's lab upstream, its policies and a client for each attack.

Lab-only, like seed-demo it is a command the database owner runs; unlike it, it refuses to run
unless LAB_MUTABLE_UPSTREAM=yes (the same second switch the lab upstream itself needs). The gate and
the file format are tested without a database; the registration, with one.
Harborline Supply Co. is fictional."""

import json
from pathlib import Path

import anyio
import psycopg
import pytest

from ai_gateway.admin import cli

REPO_ROOT = Path(__file__).resolve().parents[2]
LAB = REPO_ROOT / "config" / "lab"


async def _seed_lab_exits(*argv: str) -> str:
    """Run the CLI in a worker thread, as the other admin tests do: it starts its own event loop,
    which a thread that already has one running (a session fixture's) does not allow."""
    with pytest.raises(SystemExit) as stopped:
        await anyio.to_thread.run_sync(cli.main, list(argv))
    return str(stopped.value)


@pytest.mark.anyio
async def test_it_refuses_to_run_without_the_lab_switch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GATEWAY_MIGRATE_DATABASE_URL", "postgresql://nobody@127.0.0.1:1/none")
    monkeypatch.delenv("LAB_MUTABLE_UPSTREAM", raising=False)

    message = await _seed_lab_exits("seed-lab")

    assert "LAB_MUTABLE_UPSTREAM" in message
    assert capsys.readouterr().out == ""


@pytest.mark.anyio
@pytest.mark.parametrize("value", ["", "no", "true", "YES", "yes "])
async def test_only_the_exact_word_yes_opens_the_gate(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("GATEWAY_MIGRATE_DATABASE_URL", "postgresql://nobody@127.0.0.1:1/none")
    monkeypatch.setenv("LAB_MUTABLE_UPSTREAM", value)

    assert "LAB_MUTABLE_UPSTREAM" in await _seed_lab_exits("seed-lab")


def test_the_generated_clients_file_loads_one_client_for_each_attack() -> None:
    clients = cli.load_lab_clients(LAB / "clients.lab.toml")

    assert len(clients) >= 31
    assert len({c.name for c in clients}) == len(clients)
    assert all(c.scopes and c.name.startswith(c.original) for c in clients)


@pytest.mark.parametrize(
    "text",
    [
        '[[client]]\nname = "Bad Name"\nscopes = ["a__b"]\n',
        '[[client]]\nname = "good-name"\nscopes = []\n',
        '[[client]]\nname = "good-name"\nscopes = ["a__b"]\nextra = 1\n',
        '[[client]]\nname = "good-name"\nscopes = "a__b"\n',
        'client = "x"\n',
    ],
)
def test_a_mistake_in_the_clients_file_is_one_line_not_a_traceback(
    tmp_path: Path, text: str
) -> None:
    path = tmp_path / "clients.toml"
    path.write_text(text, encoding="utf-8")

    with pytest.raises(cli.AdminError):
        cli.load_lab_clients(path)


# -- registration, against a database -------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.anyio
async def test_it_registers_the_upstream_the_policies_and_the_clients_and_revokes_old_tokens(
    monkeypatch: pytest.MonkeyPatch,
    test_database_url: str,
    clean_database: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("GATEWAY_MIGRATE_DATABASE_URL", test_database_url)
    monkeypatch.setenv("LAB_MUTABLE_UPSTREAM", "yes")

    async def seed() -> dict[str, str]:
        await anyio.to_thread.run_sync(cli.main, ["seed-lab"])
        printed: dict[str, str] = json.loads(capsys.readouterr().out)
        return printed

    first = await seed()
    second = await seed()

    clients = cli.load_lab_clients(LAB / "clients.lab.toml")
    assert set(first) == set(second) == {c.name for c in clients}
    assert all(first[name] != second[name] for name in first), "a fresh token each time"
    async with await psycopg.AsyncConnection.connect(test_database_url) as connection:
        upstream = await (
            await connection.execute(
                "SELECT url, credential_env FROM upstream_servers WHERE namespace = 'lab'"
            )
        ).fetchone()
        live = await (
            await connection.execute(
                "SELECT max(n) FROM (SELECT count(*) AS n FROM client_tokens t"
                " JOIN clients c ON c.id = t.client_id WHERE c.name LIKE '%--%'"
                " AND t.revoked_at IS NULL GROUP BY c.id) x"
            )
        ).fetchone()
        effects = await (
            await connection.execute(
                "SELECT tool, effect FROM tool_policies WHERE namespace = 'lab'"
            )
        ).fetchall()
    async with await psycopg.AsyncConnection.connect(test_database_url) as connection:
        never = await (
            await connection.execute(
                "SELECT count(*) FROM client_tokens t JOIN clients c ON c.id = t.client_id"
                " WHERE c.name LIKE '%--%' AND t.revoked_at IS NULL"
                " AND (t.expires_at IS NULL OR t.expires_at > now() + interval '25 hours')"
            )
        ).fetchone()
    assert never == (0,), "a lab client's token expires within a day, so a stray one cannot linger"
    assert upstream == ("http://lab-upstream:4413/mcp", "LAB_UPSTREAM_SERVICE_TOKEN")
    assert live == (1,), "the first run's tokens were revoked"
    assert {row[0]: row[1] for row in effects}["forward_note"] == "write"
