"""A database for agent-core's audit log and approval queue that cannot starve the rest of the
gateway.

agent-core's storage is async: a Postgres `Database` owns a psycopg connection pool. This subclass
caps that pool at a few connections and says when every one is taken (`busy`), so a write that
needs the audit log is refused at once instead of queueing behind a stalled one.

A caller that stops waiting (a timeout, a cancelled request) closes its connection, but the server
may keep a statement running, for example one waiting for the audit append lock. What ends it is
the server's own limits (`policy_url` sets a lock, a statement and a transaction timeout on every
connection), so the database gives up a wait before the caller stops waiting for it.
"""

from collections.abc import Awaitable, Callable
from typing import TypeVar

from aox_agent_core.storage import PostgresDatabase, Session
from pydantic import SecretStr

ResultT = TypeVar("ResultT")


class BoundedPostgresDatabase(PostgresDatabase):
    def __init__(self, url: SecretStr, *, concurrency: int) -> None:
        super().__init__(url, max_connections=concurrency)
        self._concurrency = concurrency
        self._running = 0

    @property
    def busy(self) -> bool:
        """Every connection is taken: a new operation would wait for one."""
        return self._running >= self._concurrency

    async def run(
        self,
        work: Callable[[Session], Awaitable[ResultT]],
        *,
        write: bool = False,
        acquire_timeout: float | None = None,
    ) -> ResultT:
        self._running += 1
        try:
            return await super().run(work, write=write, acquire_timeout=acquire_timeout)
        finally:
            self._running -= 1
