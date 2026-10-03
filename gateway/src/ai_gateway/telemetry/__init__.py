"""Telemetry: spans and decision records, stored in Postgres for the dashboard.

The `telemetry` schema has its own roles, so the gateway can only append to it and the
dashboard can only read a few views of it. Nothing here ever holds tool arguments, tool
results, credentials or client addresses: the tables have no column for them.
"""

MIGRATIONS_PACKAGE = __name__
"""Where the telemetry schema's numbered SQL migrations live, for mcp_common.migrate."""

SCHEMA = "telemetry"
TABLES = ("requests", "layer_verdicts", "auth_failures", "spans", "pipeline_configs")
"""Every table, in the order the writer inserts into them."""
PURGEABLE_TABLES = ("requests", "layer_verdicts", "auth_failures", "spans")
"""The tables whose old rows the purge deletes; `pipeline_configs` is tiny and kept."""
WRITER_ROLE = "telemetry_writer"
READER_ROLE = "telemetry_reader"
PURGER_ROLE = "telemetry_purger"
