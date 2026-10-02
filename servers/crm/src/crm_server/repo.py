"""Queries against the crm schema, as the crm_app role.

Every SELECT names its columns, and the role can read only the public ones: the internal
columns are never selected, and a `SELECT *` would be refused by the database. The output
models are a second wall.
"""

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from crm_server.models import (
    MAX_NOTES_RETURNED,
    AccountDetail,
    AccountSummary,
    ActivityNote,
    Contact,
    Deal,
    ListDealsInput,
    SearchAccountsInput,
)
from mcp_common.toolset import ToolError

_SUMMARY_COLUMNS = "id, name, industry, region, tier, account_manager"

# A match in the name ranks above one only in the industry or the description.
_SEARCH_ACCOUNTS = f"""
SELECT {_SUMMARY_COLUMNS}
FROM accounts
WHERE (name ILIKE %(pattern)s ESCAPE '\\'
       OR industry ILIKE %(pattern)s ESCAPE '\\'
       OR about ILIKE %(pattern)s ESCAPE '\\')
  AND (%(tier)s::text IS NULL OR tier = %(tier)s)
  AND (%(region)s::text IS NULL OR region = %(region)s)
ORDER BY (name ILIKE %(pattern)s ESCAPE '\\') DESC, name, id
LIMIT %(limit)s OFFSET %(offset)s
"""  # noqa: S608 - the column list is a constant; every value is a bound parameter

_GET_ACCOUNT = """
SELECT id, name, industry, region, tier, account_manager, about, created_at
FROM accounts
WHERE id = %s
"""

_CONTACTS = """
SELECT id, full_name, title, email, phone FROM contacts WHERE account_id = %s ORDER BY id
"""

_NEWEST_NOTES = """
SELECT id, kind, author, deal_id, body, occurred_at
FROM activity_notes
WHERE account_id = %(account_id)s
ORDER BY occurred_at DESC, id DESC
LIMIT %(limit)s
"""

_NOTE_COUNT = "SELECT count(*) FROM activity_notes WHERE account_id = %s"

_LIST_DEALS = """
SELECT id, account_id, name, stage, amount_cents, close_date, owner
FROM deals
WHERE (%(account_id)s::text IS NULL OR account_id = %(account_id)s)
  AND (%(stage)s::text IS NULL OR stage = %(stage)s)
ORDER BY close_date DESC, id DESC
LIMIT %(limit)s OFFSET %(offset)s
"""


def account_not_found(account_id: str) -> ToolError:
    return ToolError(f"Account '{account_id}' was not found.")


def like_pattern(query: str) -> str:
    """The query as a contains-pattern, with LIKE's own wildcards matched literally."""
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


class CrmRepo:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def search_accounts(self, query: SearchAccountsInput) -> list[AccountSummary]:
        parameters = {**query.model_dump(), "pattern": like_pattern(query.query)}
        async with self._pool.connection() as connection:
            cursor = connection.cursor(row_factory=dict_row)
            await cursor.execute(_SEARCH_ACCOUNTS, parameters)
            return [AccountSummary.model_validate(row) for row in await cursor.fetchall()]

    async def get_account(self, account_id: str) -> AccountDetail:
        async with self._pool.connection() as connection:
            cursor = connection.cursor(row_factory=dict_row)
            await cursor.execute(_GET_ACCOUNT, (account_id,))
            account = await cursor.fetchone()
            if account is None:
                raise account_not_found(account_id)
            await cursor.execute(_CONTACTS, (account_id,))
            contacts = [Contact.model_validate(row) for row in await cursor.fetchall()]
            await cursor.execute(
                _NEWEST_NOTES, {"account_id": account_id, "limit": MAX_NOTES_RETURNED}
            )
            notes = [ActivityNote.model_validate(row) for row in await cursor.fetchall()]
            await cursor.execute(_NOTE_COUNT, (account_id,))
            total = await cursor.fetchone()
        if total is None:
            raise RuntimeError("count(*) returned no row")
        return AccountDetail.model_validate(
            {**account, "contacts": contacts, "notes": notes, "notes_total": total["count"]}
        )

    async def list_deals(self, query: ListDealsInput) -> list[Deal]:
        async with self._pool.connection() as connection:
            cursor = connection.cursor(row_factory=dict_row)
            await cursor.execute(_LIST_DEALS, query.model_dump())
            return [Deal.model_validate(row) for row in await cursor.fetchall()]
