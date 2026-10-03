"""Put the telemetry pieces together for a running gateway."""

import logging
from dataclasses import dataclass

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider

from ai_gateway.settings import GatewaySettings
from ai_gateway.telemetry.buffer import TelemetryBuffer
from ai_gateway.telemetry.sinks import PostgresEventSink
from ai_gateway.telemetry.spans import BufferSpanProcessor
from ai_gateway.telemetry.writer import TelemetryWriter, WriterStatus

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Telemetry:
    buffer: TelemetryBuffer
    writer: TelemetryWriter
    sink: PostgresEventSink

    def status(self) -> WriterStatus:
        return self.writer.status()


def build_telemetry(settings: GatewaySettings) -> Telemetry | None:
    """The storing pieces, or None when no telemetry database is configured: the gateway then
    runs as it did before, with its events in the log and no spans recorded."""
    if settings.telemetry_database_url is None:
        return None
    buffer = TelemetryBuffer(settings.telemetry_buffer_size)
    writer = TelemetryWriter(
        buffer,
        settings.telemetry_database_url.get_secret_value(),
        batch_size=settings.telemetry_batch_size,
        flush_interval_s=settings.telemetry_flush_interval_s,
    )
    return Telemetry(buffer, writer, PostgresEventSink(buffer))


def install_tracing(buffer: TelemetryBuffer) -> BufferSpanProcessor:
    """Make spans real and queue the gateway's own for storage.

    OpenTelemetry lets the tracer provider be set once per process. If something else already
    set one that is not the SDK's, spans cannot be stored and a warning says so."""
    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        provider = TracerProvider(resource=Resource.create({"service.name": "ai-gateway"}))
        trace.set_tracer_provider(provider)
        provider = trace.get_tracer_provider()
    processor = BufferSpanProcessor(buffer)
    if isinstance(provider, TracerProvider):
        provider.add_span_processor(processor)
    else:
        logger.warning("another tracer provider is installed; spans will not be stored")
    return processor
