"""The in-memory queue between the request path and the database writer.

Putting a row in costs a lock and an append; nothing on the request path ever waits for the
database. The queue is bounded: when it is full the oldest row is dropped and counted, so a
database that stays down costs memory only up to the bound and never slows a request.

Failed authentications have a quota of their own. Anyone who can reach the gateway can fail
authentication as fast as they like, and sharing one queue with the decision records would let
them choose which records survive an outage; in their own quota they can only evict each other.
"""

import threading
from collections import deque
from dataclasses import dataclass
from typing import Any

TABLES = ("requests", "layer_verdicts", "auth_failures", "spans", "pipeline_configs")
NOISY_TABLES = frozenset({"auth_failures"})
"""Tables whose rows an outsider can create at will; they are queued apart, and taken last."""
DEFAULT_NOISY_CAPACITY = 1_000


@dataclass(frozen=True)
class Row:
    table: str
    values: dict[str, Any]


class TelemetryBuffer:
    def __init__(self, capacity: int, noisy_capacity: int = DEFAULT_NOISY_CAPACITY) -> None:
        if capacity < 1 or noisy_capacity < 1:
            raise ValueError("a buffer needs room for at least one row")
        self._rows: deque[Row] = deque()
        self._noisy: deque[Row] = deque()
        self._capacity = capacity
        self._noisy_capacity = noisy_capacity
        self._lock = threading.Lock()
        self.dropped_total = 0
        """Rows thrown away because the queue was full."""

    def put(self, *rows: Row) -> None:
        with self._lock:
            for row in rows:
                queue, capacity = (
                    (self._noisy, self._noisy_capacity)
                    if row.table in NOISY_TABLES
                    else (self._rows, self._capacity)
                )
                if len(queue) >= capacity:
                    queue.popleft()
                    self.dropped_total += 1
                queue.append(row)

    def take(self, limit: int) -> list[Row]:
        """Remove and return up to `limit` of the oldest rows, decision records first."""
        with self._lock:
            taken: list[Row] = []
            for queue in (self._rows, self._noisy):
                while queue and len(taken) < limit:
                    taken.append(queue.popleft())
            return taken

    def __len__(self) -> int:
        return len(self._rows) + len(self._noisy)
