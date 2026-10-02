"""Settings shared by the gateway and every MCP server.

A service subclasses one of these and sets its own `env_prefix` (and its default port), so
the variable names stay per service: TICKETING_PORT, CRM_PORT, GATEWAY_PORT.
"""

from typing import Annotated

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode


class ServiceSettings(BaseSettings):
    """What every HTTP service of this repository is configured with."""

    bind: str = "127.0.0.1"
    port: Annotated[int, Field(ge=1, le=65535)]
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


class ServerSettings(ServiceSettings):
    """An MCP server: its own database role, and the credential the gateway must present."""

    database_url: SecretStr
    """Connection string for the server's own database role. Secret: it carries a password."""
    service_token: SecretStr
    """The credential the gateway must present. The server refuses to start without it."""
