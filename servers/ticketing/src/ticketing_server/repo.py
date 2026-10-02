"""Queries against the ticketing schema, as the ticketing_app role.

Every SELECT names its columns. Internal notes and internal comments are never selected,
so they cannot reach an output model even by a coding slip; the models are a second wall.
"""

from psycopg import errors
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from mcp_common.toolset import ToolError
from ticketing_server.models import (
    Assignment,
    CommentRef,
    ListTicketsInput,
    PublicComment,
    StatusChange,
    TicketDetail,
    TicketRef,
    TicketSummary,
)

_SUMMARY_COLUMNS = "id, account_id, subject, status, priority, assignee, created_at, updated_at"

_LIST_TICKETS = f"""
SELECT {_SUMMARY_COLUMNS}
FROM tickets
WHERE (%(status)s::text IS NULL OR status = %(status)s)
  AND (%(priority)s::text IS NULL OR priority = %(priority)s)
  AND (%(account_id)s::text IS NULL OR account_id = %(account_id)s)
ORDER BY updated_at DESC, id DESC
LIMIT %(limit)s
"""  # noqa: S608 - the column list is a constant; every value is a bound parameter

_GET_TICKET = f"""
SELECT {_SUMMARY_COLUMNS}, description, requested_by
FROM tickets
WHERE id = %s
"""  # noqa: S608

MAX_COMMENTS_RETURNED = 20

# The newest comments, shown oldest first so they read as a conversation.
_RECENT_PUBLIC_COMMENTS = """
SELECT id, author, body, created_at
FROM (
    SELECT id, author, body, created_at
    FROM comments
    WHERE ticket_id = %(ticket_id)s AND visibility = 'public'
    ORDER BY id DESC
    LIMIT %(limit)s
) AS recent
ORDER BY id
"""

_PUBLIC_COMMENT_COUNT = """
SELECT count(*) FROM comments WHERE ticket_id = %s AND visibility = 'public'
"""


def ticket_not_found(ticket_id: str) -> ToolError:
    return ToolError(f"Ticket '{ticket_id}' was not found.")


class TicketRepo:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def list_tickets(self, query: ListTicketsInput) -> list[TicketSummary]:
        async with self._pool.connection() as connection:
            cursor = connection.cursor(row_factory=dict_row)
            await cursor.execute(_LIST_TICKETS, query.model_dump())
            return [TicketSummary.model_validate(row) for row in await cursor.fetchall()]

    async def get_ticket(self, ticket_id: str) -> TicketDetail:
        async with self._pool.connection() as connection:
            cursor = connection.cursor(row_factory=dict_row)
            await cursor.execute(_GET_TICKET, (ticket_id,))
            ticket = await cursor.fetchone()
            if ticket is None:
                raise ticket_not_found(ticket_id)
            await cursor.execute(
                _RECENT_PUBLIC_COMMENTS, {"ticket_id": ticket_id, "limit": MAX_COMMENTS_RETURNED}
            )
            comments = [PublicComment.model_validate(row) for row in await cursor.fetchall()]
            await cursor.execute(_PUBLIC_COMMENT_COUNT, (ticket_id,))
            total = await cursor.fetchone()
        if total is None:
            raise RuntimeError("count(*) returned no row")
        return TicketDetail.model_validate(
            {**ticket, "comments": comments, "comments_total": total["count"]}
        )

    async def create_ticket(
        self, subject: str, description: str, priority: str, account_id: str, requested_by: str
    ) -> TicketRef:
        async with self._pool.connection() as connection:
            cursor = await connection.execute(
                "INSERT INTO tickets (subject, description, priority, account_id, requested_by)"
                " VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (subject, description, priority, account_id, requested_by),
            )
            row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("INSERT ... RETURNING returned no row")
        return TicketRef(ticket_id=row[0])

    async def add_comment(self, ticket_id: str, body: str, requested_by: str) -> CommentRef:
        """Add a public comment. The server never sets visibility: its database role cannot,
        and the column defaults to public."""
        async with self._pool.connection() as connection, connection.transaction():
            try:
                cursor = await connection.execute(
                    "INSERT INTO comments (ticket_id, author, body, requested_by)"
                    " VALUES (%s, %s, %s, %s) RETURNING id",
                    (ticket_id, requested_by, body, requested_by),
                )
            except errors.ForeignKeyViolation:
                raise ticket_not_found(ticket_id) from None
            row = await cursor.fetchone()
            await connection.execute(
                "UPDATE tickets SET updated_at = now() WHERE id = %s", (ticket_id,)
            )
        if row is None:
            raise RuntimeError("INSERT ... RETURNING returned no row")
        return CommentRef(ticket_id=ticket_id, comment_id=row[0])

    async def change_status(self, ticket_id: str, status: str) -> StatusChange:
        async with self._pool.connection() as connection:
            cursor = await connection.execute(
                "UPDATE tickets SET status = %s, updated_at = now() WHERE id = %s RETURNING status",
                (status, ticket_id),
            )
            row = await cursor.fetchone()
        if row is None:
            raise ticket_not_found(ticket_id)
        return StatusChange.model_validate({"ticket_id": ticket_id, "status": row[0]})

    async def assign(self, ticket_id: str, assignee: str) -> Assignment:
        async with self._pool.connection() as connection:
            try:
                cursor = await connection.execute(
                    "UPDATE tickets SET assignee = %s, updated_at = now() WHERE id = %s"
                    " RETURNING assignee",
                    (assignee, ticket_id),
                )
            except errors.ForeignKeyViolation:
                raise ToolError(f"Staff member '{assignee}' was not found.") from None
            row = await cursor.fetchone()
        if row is None:
            raise ticket_not_found(ticket_id)
        return Assignment(ticket_id=ticket_id, assignee=row[0])
