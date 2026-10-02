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
    git_commit: str | None = None
    """Set from the image's GIT_COMMIT build argument; None when it was not given."""
    git_branch: str | None = None
    """Set from the image's GIT_BRANCH build argument; None when it was not given."""

    @field_validator("git_commit", "git_branch", mode="before")
    @classmethod
    def _empty_means_unknown(cls, value: object) -> object:
        # An image built without the argument carries an empty string.
        return None if value == "" else value

    @field_validator("allowed_hosts", mode="before")
    @classmethod
    def _split_comma_list(cls, value: object) -> object:
        if isinstance(value, str):
            return [host.strip() for host in value.split(",") if host.strip()]
        return value
