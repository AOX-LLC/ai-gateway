"""CRM server settings, read from CRM_* environment variables."""

from typing import Annotated

from pydantic import Field
from pydantic_settings import SettingsConfigDict

from mcp_common.settings import ServerSettings


class CrmSettings(ServerSettings):
    model_config = SettingsConfigDict(env_prefix="CRM_", extra="ignore")

    port: Annotated[int, Field(ge=1, le=65535)] = 4411
