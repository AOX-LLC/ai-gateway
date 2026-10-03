"""A database for agent-core's audit log that cannot starve the rest of the gateway.

agent-core runs each database operation on the event loop's default thread pool
(`asyncio.to_thread`), and that pool is shared: it also resolves names for psycopg and httpx. A few
audit operations stuck behind a held lock would delay a read that needs a new connection. This
subclass runs the same operations on anyio's own worker threads, a few at a time, so a stalled audit
log can fill only its own limiter, and can say so (`busy`) so a write is refused at once instead of
queueing.
"""

from collections.abc import Callable
from functools import partial
from typing import TypeVar

import anyio
from aox_agent_core.storage import PostgresDatabase, Session
from pydantic import SecretStr

ResultT = TypeVar("ResultT")


class BoundedPostgresDatabase(PostgresDatabase):
    def __init__(self, url: SecretStr, *, concurrency: int) -> None:
        super().__init__(url)
        self._limiter = anyio.CapacityLimiter(concurrency)

    @property
    def busy(self) -> bool:
        """Every worker is taken: a new operation would wait for one."""
        return self._limiter.available_tokens == 0

    async def run(self, work: Callable[[Session], ResultT], *, write: bool = False) -> ResultT:
        return await anyio.to_thread.run_sync(
            partial(self.run_sync, work, write=write), limiter=self._limiter
        )
