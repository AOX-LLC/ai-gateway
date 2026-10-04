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
    approval_roles_file: Path = Path("config/approval_roles.toml")
    """The role that must approve each write tool. Read at startup with a policy database."""
    allowlist_file: Path = Path("config/allowlist.toml")
    """Per-client value rules on tool arguments (the allowlist layer)."""
    rate_limits_file: Path = Path("config/rate_limits.toml")
    """Token buckets per client for reads, writes and single tools (the rate limit layer)."""
    tool_pins_file: Path = Path("config/tool_pins.toml")
    """The reviewed description and input schema of every tool, pinned by hash (the schema and
    pinned_descriptions layers). Read once at startup; a mistake stops the gateway starting."""
    egress_file: Path = Path("config/egress.toml")
    """What the egress layer counts as data read, and how much of it a write may carry."""
    canaries_file: Path = Path("config/canaries.toml")
    """The canary values seeded in the data, as hashes (the canary layer)."""
    session_idle_timeout_s: Annotated[float, Field(gt=0)] = 900.0
    login_failures_per_id: Annotated[int, Field(ge=1)] = 5
    """Failed logins for one token id, within the window, before that id is refused."""
    login_window_s: Annotated[float, Field(gt=0)] = 60.0
    login_lockout_s: Annotated[float, Field(gt=0)] = 60.0
    login_known_good_ttl_s: Annotated[float, Field(gt=0)] = 900.0
    """Past the ceiling, a token id is still served if it logged in successfully this recently."""
    login_global_ceiling: Annotated[int, Field(ge=1)] = 200
    """Failures of any kind within the window after which only recently working ids are served."""
    max_sessions: Annotated[int, Field(ge=1)] = 1000
    max_sessions_per_client: Annotated[int, Field(ge=1)] = 20
    catalog_refresh_s: Annotated[float, Field(gt=0)] = 60.0
    catalog_registry_poll_s: Annotated[float, Field(gt=0)] = 5.0
    """How often the gateway looks for a changed upstream in the registry."""
    telemetry_database_url: SecretStr | None = None
    """Connection string for the telemetry_writer role. Without it nothing is stored: events go
    to the log only and no span is recorded. Secret because it carries a password."""
    approval_hold_s: Annotated[float, Field(ge=0, le=120)] = 45.0
    """How long a write waits for a person's decision before the client is told to retry."""
    approval_poll_s: Annotated[float, Field(gt=0, le=10)] = 1.0
    approval_max_holds: Annotated[int, Field(ge=0, le=256)] = 16
    """Writes held waiting for a decision at once, all clients together."""
    approval_max_holds_per_client: Annotated[int, Field(ge=0, le=256)] = 4
    """Of those, how many one client may hold: one client cannot fill every wait slot."""
    approval_ttl_s: Annotated[int, Field(ge=1, le=7 * 24 * 3600)] = 30 * 60
    """How long a request stays open. After it, the call has to be asked for again."""
    policy_database_url: SecretStr | None = None
    """Connection string for the policy_gateway role: the audit log (and, from Phase 3b, the
    approval queue). Without it the audit log is disabled and writes are refused, unless the
    pipeline configuration allows unaudited writes. Secret because it carries a password."""
    telemetry_buffer_size: Annotated[int, Field(ge=100)] = 10_000
    """Rows held in memory for the writer; when the database is down the oldest are dropped."""
    telemetry_batch_size: Annotated[int, Field(ge=1, le=5_000)] = 200
    telemetry_flush_interval_s: Annotated[float, Field(gt=0)] = 1.0
