"""A span processor that queues the gateway's own finished spans for the writer."""

import logging
import time

from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor

from ai_gateway.telemetry.buffer import TelemetryBuffer
from ai_gateway.telemetry.rows import span_row

logger = logging.getLogger(__name__)

_LOG_EVERY_S = 60.0


class BufferSpanProcessor(SpanProcessor):
    """The SDK calls `on_end` from the code that ended the span, with no guard, so an
    exception here would surface in a request. Nothing may escape it."""

    def __init__(self, buffer: TelemetryBuffer) -> None:
        self._buffer = buffer
        self._closed = False
        self._last_logged: float | None = None

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        return None

    def on_end(self, span: ReadableSpan) -> None:
        if self._closed:
            return
        try:
            row = span_row(span)
            if row is not None:
                self._buffer.put(row)
        except Exception:
            now = time.monotonic()
            if self._last_logged is None or now - self._last_logged >= _LOG_EVERY_S:
                self._last_logged = now
                logger.exception("a span could not be queued for storage")

    def shutdown(self) -> None:
        self._closed = True

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True
