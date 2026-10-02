"""Ticketing server settings, read from TICKETING_* environment variables."""

from typing import Annotated

from pydantic import Field
from pydantic_settings import SettingsConfigDict

from mcp_common.settings import ServerSettings


class TicketingSettings(ServerSettings):
    model_config = SettingsConfigDict(env_prefix="TICKETING_", extra="ignore")

    port: Annotated[int, Field(ge=1, le=65535)] = 4412
