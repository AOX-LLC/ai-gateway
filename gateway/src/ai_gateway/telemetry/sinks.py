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
    first failure is logged in full, then at most one line a minute with a count, and says which
    request the failed event belonged to."""

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
                    "event sink %s failed for request %s (%d since the last report)",
                    self._name,
                    event.payload.get("request_id"),
                    self._failures_since_log,
                )
                self._last_logged = now
                self._failures_since_log = 0


class FanOutEventSink:
    """Sends each event to the `extra` sinks, then to the `primary` one.

    The extra sinks are guarded: one that fails is logged and does not stop the rest. The primary
    sink (the log today, the audit log in Phase 3) is not wrapped, so its failure reaches the
    pipeline, which logs it with the request id: a missing audit record must stay traceable to its
    request, and rate limiting that log would hide it. Extra sinks that never wait come first, so a
    primary sink that stalls cannot hold them up."""

    def __init__(self, extra: Sequence[tuple[str, EventSink]], primary: EventSink) -> None:
        self._extra = [SafeEventSink(sink, name) for name, sink in extra]
        self._primary = primary

    async def emit(self, event: GatewayEvent) -> None:
        for sink in self._extra:
            await sink.emit(event)
        await self._primary.emit(event)
