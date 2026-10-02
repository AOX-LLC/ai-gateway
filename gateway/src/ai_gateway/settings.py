"""Gateway settings, read from GATEWAY_* environment variables."""

from pathlib import Path
from typing import Annotated

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class GatewaySettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GATEWAY_", extra="ignore")

    database_url: SecretStr
    """Connection string for the least-privilege gateway_app role. Secret because it
    carries a password; never logged."""

    bind: str = "127.0.0.1"
    port: Annotated[int, Field(ge=1, le=65535)] = 4401
    pipeline_file: Path = Path("config/pipeline.toml")
    allowed_hosts: Annotated[list[str], NoDecode] = ["127.0.0.1:*", "localhost:*"]
    """Host headers the MCP endpoint accepts (the SDK's DNS-rebinding protection)."""
    log_level: str = "INFO"
    session_idle_timeout_s: Annotated[float, Field(gt=0)] = 900.0
    max_sessions: Annotated[int, Field(ge=1)] = 1000
    catalog_refresh_s: Annotated[float, Field(gt=0)] = 60.0

    @field_validator("allowed_hosts", mode="before")
    @classmethod
    def _split_comma_list(cls, value: object) -> object:
        if isinstance(value, str):
            return [host.strip() for host in value.split(",") if host.strip()]
        return value
