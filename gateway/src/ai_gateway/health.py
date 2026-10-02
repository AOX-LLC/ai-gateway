"""What /healthz reports: the running build and the database schema it is serving."""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from psycopg import Error as DatabaseError

logger = logging.getLogger(__name__)

COMMIT_SOURCE = "process_start"
"""The commit is fixed when the process starts: it is baked into the image at build time."""

SCHEMA_VERSION_CACHE_S = 30.0

HealthPayload = dict[str, str | float | None]


class SchemaVersionSource(Protocol):
    async def schema_version(self) -> int: ...


@dataclass(frozen=True)
class BuildIdentity:
    commit: str | None
    """None when the image was built without a commit, e.g. outside a git checkout."""
    branch: str | None
    version: str
    started_at: float = field(default_factory=time.monotonic)

    def payload(self, status: str, schema_version: str | None) -> HealthPayload:
        return {
            "status": status,
            "commit": self.commit,
            "commit_source": COMMIT_SOURCE,
            "branch": self.branch,
            "version": self.version,
            "schema_version": schema_version,
            "uptime_s": round(time.monotonic() - self.started_at, 1),
        }


@dataclass
class SchemaVersionCache:
    """The applied schema version as a zero-padded string ("0002"), re-read at most every
    ttl_s, so /healthz notices a migration without a restart and stays cheap.

    A failed read reports None, and is cached like a success so a database outage does
    not turn every health check into another failing query.
    """

    source: SchemaVersionSource
    ttl_s: float = SCHEMA_VERSION_CACHE_S
    clock: Callable[[], float] = time.monotonic
    _value: str | None = field(default=None, init=False)
    _read_at: float | None = field(default=None, init=False)

    async def current(self) -> str | None:
        now = self.clock()
        if self._read_at is not None and now - self._read_at < self.ttl_s:
            return self._value

        try:
            self._value = f"{await self.source.schema_version():04d}"
        except DatabaseError:
            logger.warning("could not read the schema version", exc_info=True)
            self._value = None
        self._read_at = now
        return self._value
