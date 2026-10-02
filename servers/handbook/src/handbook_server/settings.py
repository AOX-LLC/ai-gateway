"""Handbook server settings, read from HANDBOOK_* environment variables."""

from pathlib import Path
from typing import Annotated

from pydantic import Field
from pydantic_settings import SettingsConfigDict

from mcp_common.settings import ServerSettings


class HandbookSettings(ServerSettings):
    model_config = SettingsConfigDict(env_prefix="HANDBOOK_", extra="ignore")

    model_path: Path
    """The directory holding the embedding model (see scripts/fetch_model.py). The server
    never downloads it: a missing directory stops the start."""
    port: Annotated[int, Field(ge=1, le=65535)] = 4410
