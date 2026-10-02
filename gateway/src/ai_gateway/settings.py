"""Gateway settings, read from GATEWAY_* environment variables."""

from pathlib import Path
from typing import Annotated

from pydantic import Field, SecretStr
from pydantic_settings import SettingsConfigDict

from mcp_common.settings import ServiceSettings


class GatewaySettings(ServiceSettings):
    model_config = SettingsConfigDict(env_prefix="GATEWAY_", extra="ignore")

    database_url: SecretStr
    """Connection string for the least-privilege gateway_app role. Secret because it
    carries a password; never logged."""

    port: Annotated[int, Field(ge=1, le=65535)] = 4401
    pipeline_file: Path = Path("config/pipeline.toml")
    session_idle_timeout_s: Annotated[float, Field(gt=0)] = 900.0
    max_sessions: Annotated[int, Field(ge=1)] = 1000
    max_sessions_per_client: Annotated[int, Field(ge=1)] = 20
    catalog_refresh_s: Annotated[float, Field(gt=0)] = 60.0
    telemetry_database_url: SecretStr | None = None
    """Connection string for the telemetry_writer role. Without it nothing is stored: events go
    to the log only and no span is recorded. Secret because it carries a password."""
    telemetry_buffer_size: Annotated[int, Field(ge=100)] = 10_000
    """Rows held in memory for the writer; when the database is down the oldest are dropped."""
    telemetry_batch_size: Annotated[int, Field(ge=1, le=5_000)] = 200
    telemetry_flush_interval_s: Annotated[float, Field(gt=0)] = 1.0
