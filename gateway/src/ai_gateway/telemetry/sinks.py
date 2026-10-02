"""Event sinks for telemetry: store records, fan out to several sinks, and survive failures."""

import logging
import time
from collections.abc import Sequence

from ai_gateway.seams.events import EventSink, GatewayEvent
from ai_gateway.telemetry.buffer import TelemetryBuffer
from ai_gateway.telemetry.rows import rows_for_event

logger = logging.getLogger(__name__)

_LOG_EVERY_S = 60.0


class PostgresEventSink:
    """Turns each event into rows and queues them. It never waits for the database."""

    def __init__(self, buffer: TelemetryBuffer) -> None:
        self._buffer = buffer

    async def emit(self, event: GatewayEvent) -> None:
        self._buffer.put(*rows_for_event(event))


class SafeEventSink:
    """Wraps a sink so that its failure is logged, not raised. The log is rate limited: the
    first failure is logged in full, then at most one line a minute with a count."""

    def __init__(self, sink: EventSink, name: str) -> None:
        self._sink = sink
        self._name = name
        self._failures_since_log = 0
        self._last_logged: float | None = None

    async def emit(self, event: GatewayEvent) -> None:
        try:
            await self._sink.emit(event)
        except Exception:
            self._failures_since_log += 1
            now = time.monotonic()
            if self._last_logged is None or now - self._last_logged >= _LOG_EVERY_S:
                logger.exception(
                    "event sink %s failed (%d since the last report)",
                    self._name,
                    self._failures_since_log,
                )
                self._last_logged = now
                self._failures_since_log = 0


class FanOutEventSink:
    """Sends each event to every sink, in order. A sink that fails does not stop the ones
    after it. Put a sink that awaits (and so can stall) after the ones that do not."""

    def __init__(self, sinks: Sequence[tuple[str, EventSink]]) -> None:
        self._sinks = [SafeEventSink(sink, name) for name, sink in sinks]

    async def emit(self, event: GatewayEvent) -> None:
        for sink in self._sinks:
            await sink.emit(event)
