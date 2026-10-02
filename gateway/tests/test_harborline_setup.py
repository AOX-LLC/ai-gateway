"""harborline-setup: idempotent role, schema, migration and seed, run as the owner."""

import shutil
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from crm_server.seed import ACCOUNT_COUNT, CONTACT_COUNT, DEAL_COUNT, NOTE_COUNT
from harborline_setup.crm import setup_crm
from harborline_setup.handbook import setup_handbook
from harborline_setup.handbook_documents import DocumentFileError
from harborline_setup.ticketing import setup_ticketing
from tests.helpers import HANDBOOK_DOCUMENTS
from ticketing_server.seed import COMMENT_COUNT, TICKET_COUNT

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


async def _counts(url: str) -> tuple[int, int, int]:
    async with await psycopg.AsyncConnection.connect(url) as connection:
        counts = []
        for table in ("staff", "tickets", "comments"):
            cursor = await connection.execute(f"SELECT count(*) FROM ticketing.{table}".encode())  # noqa: S608
            row = await cursor.fetchone()
            assert row is not None
            counts.append(int(row[0]))
    return counts[0], counts[1], counts[2]


async def test_setup_seeds_an_empty_schema_once_and_is_safe_to_repeat(
    test_database_url: str, ticketing_app_url: str, ticketing_schema: None
) -> None:
    # The role's password is the one the rest of the suite logs in with, so reuse it.
    password = str(conninfo_to_dict(ticketing_app_url)["password"])
    async with await psycopg.AsyncConnection.connect(test_database_url) as connection:
        await connection.execute("TRUNCATE ticketing.comments, ticketing.tickets, ticketing.staff")

    await setup_ticketing(test_database_url, password, None)
    first = await _counts(test_database_url)
    await setup_ticketing(test_database_url, password, None)
    second = await _counts(test_database_url)

    assert first == (12, TICKET_COUNT, COMMENT_COUNT)
    assert second == first


async def test_setup_refuses_an_empty_password(test_database_url: str) -> None:
    with pytest.raises(ValueError, match="TICKETING_DB_PASSWORD"):
        await setup_ticketing(test_database_url, "", None)


async def _privileges(url: str, role: str) -> dict[str, bool]:
    async with await psycopg.AsyncConnection.connect(url) as connection:
        cursor = await connection.execute(
            "SELECT has_schema_privilege(%(r)s, 'ticketing', 'USAGE'),"
            " has_table_privilege(%(r)s, 'ticketing.tickets', 'SELECT'),"
            " has_column_privilege(%(r)s, 'ticketing.comments', 'body', 'INSERT'),"
            " has_column_privilege(%(r)s, 'ticketing.comments', 'visibility', 'INSERT'),"
            " has_column_privilege(%(r)s, 'ticketing.tickets', 'internal_notes', 'INSERT'),"
            " has_column_privilege(%(r)s, 'ticketing.tickets', 'status', 'UPDATE'),"
            " has_table_privilege(%(r)s, 'ticketing.tickets', 'DELETE'),"
            " has_sequence_privilege(%(r)s, 'ticketing.ticket_number', 'USAGE')",
            {"r": role},
        )
        row = await cursor.fetchone()
    assert row is not None
    names = [
        "usage",
        "select",
        "insert_comment_body",
        "insert_visibility",
        "insert_internal_notes",
        "update_status",
        "delete",
        "sequence",
    ]
    return dict(zip(names, row, strict=True))


async def test_setup_builds_everything_from_a_database_without_the_role_or_schema(
    scratch_database: tuple[str, str], test_database_url: str
) -> None:
    url, role = scratch_database
    password = f"scratch-{uuid4().hex}"

    await setup_ticketing(url, password, None, role)
    first = await _privileges(url, role)
    first_counts = await _counts(url)
    await setup_ticketing(url, password, None, role)

    expected = {
        "usage": True,
        "select": True,
        "insert_comment_body": True,
        "insert_visibility": False,
        "insert_internal_notes": False,
        "update_status": True,
        "delete": False,
        "sequence": True,
    }
    assert first == expected
    assert await _privileges(url, role) == expected
    assert first_counts == (12, TICKET_COUNT, COMMENT_COUNT)
    assert await _counts(url) == first_counts


async def test_setup_restores_grants_that_were_revoked(
    scratch_database: tuple[str, str],
) -> None:
    url, role = scratch_database
    password = f"scratch-{uuid4().hex}"
    await setup_ticketing(url, password, None, role)
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as connection:
        await connection.execute(
            sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA ticketing FROM {}").format(
                sql.Identifier(role)
            )
        )

    await setup_ticketing(url, password, None, role)

    assert (await _privileges(url, role))["select"] is True


async def test_setup_stores_a_scram_verifier_and_the_role_can_log_in(
    scratch_database: tuple[str, str],
) -> None:
    url, role = scratch_database
    password = f"scratch-{uuid4().hex}"

    await setup_ticketing(url, password, None, role)

    async with await psycopg.AsyncConnection.connect(url) as owner:
        cursor = await owner.execute(
            "SELECT rolpassword FROM pg_authid WHERE rolname = %s", (role,)
        )
        row = await cursor.fetchone()
    assert row is not None
    assert row[0].startswith("SCRAM-SHA-256$")
    assert password not in row[0]
    as_role = make_conninfo(url, user=role, password=password)
    async with await psycopg.AsyncConnection.connect(as_role) as connection:
        cursor = await connection.execute("SELECT current_user")
        assert await cursor.fetchone() == (role,)
    with pytest.raises(psycopg.OperationalError):
        await psycopg.AsyncConnection.connect(
            make_conninfo(url, user=role, password=f"not-{password}")
        )


async def test_setup_takes_the_default_database_access_away_from_public(
    scratch_database: tuple[str, str],
) -> None:
    url, role = scratch_database

    await setup_ticketing(url, f"scratch-{uuid4().hex}", None, role)

    async with await psycopg.AsyncConnection.connect(url) as connection:
        cursor = await connection.execute(
            "SELECT has_database_privilege(0::oid, current_database(), 'CONNECT'),"
            " has_database_privilege(0::oid, current_database(), 'TEMPORARY'),"
            " has_schema_privilege(0::oid, 'public', 'USAGE'),"
            " has_database_privilege(%(r)s, current_database(), 'CONNECT'),"
            " has_schema_privilege(%(r)s, 'public', 'USAGE')",
            {"r": role},
        )
        assert await cursor.fetchone() == (False, False, False, True, False)


# --- the CRM step ---------------------------------------------------------------------------


async def _crm_counts(url: str) -> tuple[int, int, int, int]:
    async with await psycopg.AsyncConnection.connect(url) as connection:
        counts = []
        for table in ("accounts", "contacts", "deals", "activity_notes"):
            cursor = await connection.execute(f"SELECT count(*) FROM crm.{table}".encode())  # noqa: S608
            row = await cursor.fetchone()
            assert row is not None
            counts.append(int(row[0]))
    return counts[0], counts[1], counts[2], counts[3]


async def _crm_privileges(url: str, role: str) -> dict[str, bool]:
    async with await psycopg.AsyncConnection.connect(url) as connection:
        cursor = await connection.execute(
            "SELECT has_schema_privilege(%(r)s, 'crm', 'USAGE'),"
            " has_column_privilege(%(r)s, 'crm.accounts', 'name', 'SELECT'),"
            " has_column_privilege(%(r)s, 'crm.accounts', 'internal_notes', 'SELECT'),"
            " has_column_privilege(%(r)s, 'crm.accounts', 'credit_limit_internal', 'SELECT'),"
            " has_column_privilege(%(r)s, 'crm.deals', 'floor_price_cents', 'SELECT'),"
            " has_column_privilege(%(r)s, 'crm.deals', 'amount_cents', 'SELECT'),"
            " has_table_privilege(%(r)s, 'crm.accounts', 'INSERT'),"
            " has_table_privilege(%(r)s, 'crm.activity_notes', 'UPDATE')",
            {"r": role},
        )
        row = await cursor.fetchone()
    assert row is not None
    names = [
        "usage", "select_name", "select_internal_notes", "select_credit_limit",
        "select_floor_price", "select_amount", "insert", "update",
    ]  # fmt: skip
    return dict(zip(names, row, strict=True))


async def test_crm_setup_builds_everything_once_and_is_safe_to_repeat(
    scratch_database: tuple[str, str],
) -> None:
    url, role = scratch_database
    password = f"scratch-{uuid4().hex}"

    await setup_crm(url, password, None, role)
    first_counts = await _crm_counts(url)
    first_privileges = await _crm_privileges(url, role)
    await setup_crm(url, password, None, role)

    assert first_counts == (ACCOUNT_COUNT, CONTACT_COUNT, DEAL_COUNT, NOTE_COUNT)
    assert await _crm_counts(url) == first_counts
    expected = {
        "usage": True,
        "select_name": True,
        "select_internal_notes": False,
        "select_credit_limit": False,
        "select_floor_price": False,
        "select_amount": True,
        "insert": False,
        "update": False,
    }
    assert first_privileges == expected
    assert await _crm_privileges(url, role) == expected


async def test_crm_setup_narrows_a_grant_that_was_widened(
    scratch_database: tuple[str, str],
) -> None:
    url, role = scratch_database
    password = f"scratch-{uuid4().hex}"
    await setup_crm(url, password, None, role)
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as connection:
        await connection.execute(
            sql.SQL("GRANT SELECT, INSERT ON crm.accounts TO {}").format(sql.Identifier(role))
        )
    assert (await _crm_privileges(url, role))["select_internal_notes"] is True

    await setup_crm(url, password, None, role)

    privileges = await _crm_privileges(url, role)
    assert privileges["select_internal_notes"] is False
    assert privileges["insert"] is False
    assert privileges["select_name"] is True


async def test_crm_setup_refuses_an_empty_password(test_database_url: str) -> None:
    with pytest.raises(ValueError, match="CRM_DB_PASSWORD"):
        await setup_crm(test_database_url, "", None)


# --- the handbook step ----------------------------------------------------------------------


async def _handbook_counts(url: str) -> tuple[int, int]:
    async with await psycopg.AsyncConnection.connect(url) as connection:
        cursor = await connection.execute("SELECT count(*) FROM handbook.documents")
        documents = await cursor.fetchone()
        cursor = await connection.execute("SELECT count(*) FROM handbook.chunks")
        chunks = await cursor.fetchone()
    assert documents is not None
    assert chunks is not None
    return int(documents[0]), int(chunks[0])


async def _handbook_privileges(url: str, role: str) -> dict[str, bool]:
    async with await psycopg.AsyncConnection.connect(url) as connection:
        cursor = await connection.execute(
            "SELECT has_schema_privilege(%(r)s, 'handbook', 'USAGE'),"
            " has_table_privilege(%(r)s, 'handbook.published_documents', 'SELECT'),"
            " has_table_privilege(%(r)s, 'handbook.searchable_chunks', 'SELECT'),"
            " has_table_privilege(%(r)s, 'handbook.documents', 'SELECT'),"
            " has_table_privilege(%(r)s, 'handbook.chunks', 'SELECT'),"
            " has_table_privilege(%(r)s, 'handbook.searchable_chunks', 'INSERT')",
            {"r": role},
        )
        row = await cursor.fetchone()
    assert row is not None
    names = ["usage", "documents_view", "chunks_view", "documents", "chunks", "insert"]
    return dict(zip(names, row, strict=True))


async def test_handbook_setup_builds_everything_once_and_is_safe_to_repeat(
    scratch_database: tuple[str, str], model_path: Path
) -> None:
    url, role = scratch_database
    password = f"scratch-{uuid4().hex}"

    await setup_handbook(url, password, model_path, HANDBOOK_DOCUMENTS, role)
    first_counts = await _handbook_counts(url)
    first_privileges = await _handbook_privileges(url, role)
    await setup_handbook(url, password, model_path, HANDBOOK_DOCUMENTS, role)

    assert first_counts[0] == 30
    assert first_counts[1] >= 30
    assert await _handbook_counts(url) == first_counts
    expected = {
        "usage": True,
        "documents_view": True,
        "chunks_view": True,
        "documents": False,
        "chunks": False,
        "insert": False,
    }
    assert first_privileges == expected
    assert await _handbook_privileges(url, role) == expected


async def test_handbook_setup_restores_the_superseded_markers_of_an_already_seeded_schema(
    scratch_database: tuple[str, str], model_path: Path
) -> None:
    """A schema seeded before the column existed has no values, and the seed runs only when
    the schema is empty, so a repeat run must bring the markers in step with the files."""
    url, role = scratch_database
    password = f"scratch-{uuid4().hex}"
    await setup_handbook(url, password, model_path, HANDBOOK_DOCUMENTS, role)
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as connection:
        await connection.execute(
            "UPDATE handbook.documents SET superseded_by = NULL WHERE id = 'DOC-007'"
        )

    await setup_handbook(url, password, model_path, HANDBOOK_DOCUMENTS, role)

    async with await psycopg.AsyncConnection.connect(url) as connection:
        cursor = await connection.execute(
            "SELECT id, superseded_by FROM handbook.documents WHERE superseded_by IS NOT NULL"
        )
        assert await cursor.fetchall() == [("DOC-007", "DOC-008")]


async def test_handbook_setup_names_a_superseding_document_that_was_never_seeded(
    scratch_database: tuple[str, str], model_path: Path, tmp_path: Path
) -> None:
    """The seed runs only into an empty schema, so a pointer to a document added to the files
    since is an error with a clear cause, not a foreign-key failure at commit."""
    url, role = scratch_database
    password = f"scratch-{uuid4().hex}"
    await setup_handbook(url, password, model_path, HANDBOOK_DOCUMENTS, role)
    folder = tmp_path / "documents"
    shutil.copytree(HANDBOOK_DOCUMENTS, folder)
    (folder / "DOC-031.md").write_text(
        "---\nid: DOC-031\ntitle: New edition\ncategory: returns\n"
        "classification: general\nupdated: 2026-09-01\n---\n\n# New edition\n\n"
        "## Rule\n\nText.\n",
        encoding="utf-8",
    )
    superseded = folder / "DOC-016.md"
    superseded.write_text(
        superseded.read_text(encoding="utf-8").replace(
            "updated: 2025-12-01\n", "updated: 2025-12-01\nsuperseded_by: DOC-031\n"
        ),
        encoding="utf-8",
    )

    with pytest.raises(DocumentFileError, match="DOC-031, which is not in the database"):
        await setup_handbook(url, password, model_path, folder, role)


async def test_handbook_setup_puts_the_vector_extension_in_the_handbook_schema(
    scratch_database: tuple[str, str], model_path: Path
) -> None:
    url, role = scratch_database

    await setup_handbook(url, f"scratch-{uuid4().hex}", model_path, HANDBOOK_DOCUMENTS, role)

    async with await psycopg.AsyncConnection.connect(url) as connection:
        cursor = await connection.execute(
            "SELECT n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace"
            " WHERE e.extname = 'vector'"
        )
        assert await cursor.fetchall() == [("handbook",)]


async def test_handbook_setup_narrows_a_grant_widened_onto_a_base_table(
    scratch_database: tuple[str, str], model_path: Path
) -> None:
    url, role = scratch_database
    password = f"scratch-{uuid4().hex}"
    await setup_handbook(url, password, model_path, HANDBOOK_DOCUMENTS, role)
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as connection:
        await connection.execute(
            sql.SQL("GRANT SELECT ON handbook.documents, handbook.chunks TO {}").format(
                sql.Identifier(role)
            )
        )
    assert (await _handbook_privileges(url, role))["documents"] is True

    await setup_handbook(url, password, model_path, HANDBOOK_DOCUMENTS, role)

    privileges = await _handbook_privileges(url, role)
    assert privileges["documents"] is False
    assert privileges["chunks"] is False
    assert privileges["documents_view"] is True


async def test_handbook_setup_refuses_an_empty_password(
    test_database_url: str, model_path: Path
) -> None:
    with pytest.raises(ValueError, match="HANDBOOK_DB_PASSWORD"):
        await setup_handbook(test_database_url, "", model_path, HANDBOOK_DOCUMENTS)


# --- grants widened below table level are narrowed again -------------------------------------


async def _as_owner(url: str, *statements: str, role: str) -> None:
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as connection:
        for statement in statements:
            await connection.execute(sql.SQL(statement).format(sql.Identifier(role)))


async def _holds(url: str, role: str, query: str) -> bool:
    async with await psycopg.AsyncConnection.connect(url) as connection:
        cursor = await connection.execute(query.encode(), {"r": role})
        row = await cursor.fetchone()
    assert row is not None
    return bool(row[0])


async def test_ticketing_setup_narrows_column_sequence_and_schema_grants(
    scratch_database: tuple[str, str],
) -> None:
    url, role = scratch_database
    password = f"scratch-{uuid4().hex}"
    await setup_ticketing(url, password, None, role)
    checks = {
        "update the subject column": (
            "SELECT has_column_privilege(%(r)s, 'ticketing.tickets', 'subject', 'UPDATE')"
        ),
        "read the ticket number sequence": (
            "SELECT has_sequence_privilege(%(r)s, 'ticketing.ticket_number', 'SELECT')"
        ),
        "create in the schema": "SELECT has_schema_privilege(%(r)s, 'ticketing', 'CREATE')",
        "delete a ticket": "SELECT has_table_privilege(%(r)s, 'ticketing.tickets', 'DELETE')",
    }
    await _as_owner(
        url,
        "GRANT UPDATE (subject) ON ticketing.tickets TO {}",
        "GRANT SELECT ON SEQUENCE ticketing.ticket_number TO {}",
        "GRANT CREATE ON SCHEMA ticketing TO {}",
        "GRANT DELETE ON ticketing.tickets TO {}",
        role=role,
    )
    assert all([await _holds(url, role, query) for query in checks.values()])

    await setup_ticketing(url, password, None, role)

    assert {
        name: await _holds(url, role, query) for name, query in checks.items()
    } == dict.fromkeys(checks, False)
    # What the role does need is still there.
    assert await _holds(
        url, role, "SELECT has_column_privilege(%(r)s, 'ticketing.tickets', 'status', 'UPDATE')"
    )
    assert await _holds(
        url, role, "SELECT has_sequence_privilege(%(r)s, 'ticketing.ticket_number', 'USAGE')"
    )


async def test_crm_setup_narrows_a_column_and_a_schema_grant(
    scratch_database: tuple[str, str],
) -> None:
    url, role = scratch_database
    password = f"scratch-{uuid4().hex}"
    await setup_crm(url, password, None, role)
    column = "SELECT has_column_privilege(%(r)s, 'crm.accounts', 'credit_limit_internal', 'SELECT')"
    create = "SELECT has_schema_privilege(%(r)s, 'crm', 'CREATE')"
    await _as_owner(
        url,
        "GRANT SELECT (credit_limit_internal) ON crm.accounts TO {}",
        "GRANT CREATE ON SCHEMA crm TO {}",
        role=role,
    )
    assert await _holds(url, role, column)
    assert await _holds(url, role, create)

    await setup_crm(url, password, None, role)

    assert not await _holds(url, role, column)
    assert not await _holds(url, role, create)
    assert await _holds(url, role, "SELECT has_schema_privilege(%(r)s, 'crm', 'USAGE')")


async def test_handbook_setup_narrows_a_schema_grant(
    scratch_database: tuple[str, str], model_path: Path
) -> None:
    url, role = scratch_database
    password = f"scratch-{uuid4().hex}"
    await setup_handbook(url, password, model_path, HANDBOOK_DOCUMENTS, role)
    create = "SELECT has_schema_privilege(%(r)s, 'handbook', 'CREATE')"
    await _as_owner(url, "GRANT CREATE ON SCHEMA handbook TO {}", role=role)
    assert await _holds(url, role, create)

    await setup_handbook(url, password, model_path, HANDBOOK_DOCUMENTS, role)

    assert not await _holds(url, role, create)
    assert await _holds(url, role, "SELECT has_schema_privilege(%(r)s, 'handbook', 'USAGE')")
