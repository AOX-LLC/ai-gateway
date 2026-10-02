"""Ticketing server settings, read from TICKETING_* environment variables."""

from typing import Annotated

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class TicketingSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TICKETING_", extra="ignore")

    database_url: SecretStr
    """Connection string for the ticketing_app role. Secret: it carries a password."""
    service_token: SecretStr
    """The credential the gateway must present. The server refuses to start without it."""
    bind: str = "127.0.0.1"
    port: Annotated[int, Field(ge=1, le=65535)] = 4412
    allowed_hosts: Annotated[list[str], NoDecode] = ["127.0.0.1:*", "localhost:*"]
    """Host headers the MCP endpoint accepts (the SDK's DNS-rebinding protection)."""
    log_level: str = "INFO"

    @field_validator("allowed_hosts", mode="before")
    @classmethod
    def _split_comma_list(cls, value: object) -> object:
        if isinstance(value, str):
            return [host.strip() for host in value.split(",") if host.strip()]
        return value
