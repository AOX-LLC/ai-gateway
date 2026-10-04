"""Build the judge for a running gateway: agent-core's client, and where the books go."""

import logging
from datetime import UTC, datetime
from pathlib import Path

from aox_agent_core import AgentClient, load_config
from aox_agent_core.config import Mode
from aox_agent_core.errors import AgentCoreError

from ai_gateway.classifier.judge import Judge, JudgeConfig, UsageRecord
from ai_gateway.pipeline.config import PipelineConfig
from ai_gateway.pipeline.types import LayerMode
from ai_gateway.settings import GatewaySettings
from ai_gateway.telemetry.rows import model_usage_row
from ai_gateway.telemetry.runtime import Telemetry

logger = logging.getLogger(__name__)


class ClassifierSetupError(ValueError):
    pass


def build_judge(
    settings: GatewaySettings,
    config: JudgeConfig,
    telemetry: Telemetry | None,
    pipeline_config: PipelineConfig,
) -> Judge | None:
    """The judge, or None when the classifier layer is off (nothing is built, nothing is read).

    Replay mode (the default) needs a directory of recordings and no key; record and live need
    AGENT_CORE_ANTHROPIC_API_KEY, which agent-core reads itself and the gateway never sees. A
    mistake stops the gateway starting rather than leaving the layer unable to judge."""
    if pipeline_config.modes.get("classifier") is LayerMode.OFF:
        return None
    try:
        agent_config = load_config(settings.agent_core_config_file)
        client = AgentClient(agent_config)
    except AgentCoreError as error:
        raise ClassifierSetupError(
            f"the classifier's model client cannot start: {error}"
        ) from error
    if agent_config.mode is Mode.REPLAY and not Path(agent_config.replay.cassette_dir).is_dir():
        raise ClassifierSetupError(
            f"replay mode needs the recordings in {agent_config.replay.cassette_dir}, which is not"
            " a directory: restore them, or set AGENT_CORE_MODE to live with a key"
        )
    logger.info("the injection classifier runs in %s mode", agent_config.mode.value)

    def book(record: UsageRecord) -> None:
        if telemetry is not None:
            telemetry.buffer.put(model_usage_row(record, datetime.now(UTC)))

    return Judge(client, config, book)
