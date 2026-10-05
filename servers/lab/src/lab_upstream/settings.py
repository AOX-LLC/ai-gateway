"""Lab upstream settings, read from LAB_* environment variables."""

from typing import Annotated

from pydantic import Field, SecretStr
from pydantic_settings import SettingsConfigDict

from mcp_common.settings import ServiceSettings


class LabSettings(ServiceSettings):
    model_config = SettingsConfigDict(env_prefix="LAB_", extra="ignore")

    port: Annotated[int, Field(ge=1, le=65535)] = 4413
    service_token: SecretStr = SecretStr("")
    """The credential the gateway presents. Empty unless the run gave one: the server then
    refuses to start."""
    mutable_upstream: str = ""
    """Must be exactly `yes`: the second of the three switches (profile, this, the credential)."""
    phase: str = "reviewed"
    """`reviewed`, `rugpulled` or `poisoned`: which definitions the server offers."""
