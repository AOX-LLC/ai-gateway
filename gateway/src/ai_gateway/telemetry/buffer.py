"""The in-memory queue between the request path and the database writer.

Putting a row in costs a lock and an append; nothing on the request path ever waits for the
database. The queue is bounded: when it is full the oldest row is dropped and counted, so a
database that stays down costs memory only up to the bound and never slows a request.
"""

import threading
from collections import deque
from dataclasses import dataclass
from typing import Any

TABLES = ("requests", "layer_verdicts", "auth_failures", "spans", "pipeline_configs")


@dataclass(frozen=True)
class Row:
    table: str
    values: dict[str, Any]


class TelemetryBuffer:
    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("a buffer needs room for at least one row")
        self._rows: deque[Row] = deque()
        self._capacity = capacity
        self._lock = threading.Lock()
        self.dropped_total = 0
        """Rows thrown away because the queue was full."""

    def put(self, *rows: Row) -> None:
        with self._lock:
            for row in rows:
                if len(self._rows) >= self._capacity:
                    self._rows.popleft()
                    self.dropped_total += 1
                self._rows.append(row)

    def take(self, limit: int) -> list[Row]:
        """Remove and return up to `limit` of the oldest rows."""
        with self._lock:
            return [self._rows.popleft() for _ in range(min(limit, len(self._rows)))]

    def __len__(self) -> int:
        return len(self._rows)
