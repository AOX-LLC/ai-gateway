"""Load the pipeline configuration: one mode per layer, fixed at startup.

    [layers]
    scope = "enforce"        # enforce | monitor | off

    [safety]
    allow_floor_override = false     # weakening a floor layer also needs LAB_FLOOR_OVERRIDE=yes
    allow_unaudited_writes = false   # a write is refused when its audit record cannot be written

    [schema]
    validate_results = true          # a result's structured content fits the pinned output schema

Every mistake stops the gateway from starting rather than quietly weakening it: an
unknown layer name, an unknown mode, an unknown key, or a floor layer weakened without
the override flag. A layer the file does not mention runs in enforce mode.
"""

import hashlib
import json
import logging
import os
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from ai_gateway.pipeline.types import BaseLayer, LayerMode

logger = logging.getLogger(__name__)

_TOP_LEVEL_KEYS = frozenset({"layers", "safety", "schema"})
_SCHEMA_KEYS = frozenset({"validate_results"})
LAB_FLOOR_OVERRIDE_ENV = "LAB_FLOOR_OVERRIDE"
_SAFETY_KEYS = frozenset({"allow_floor_override", "allow_unaudited_writes"})


class PipelineConfigError(ValueError):
    pass


@dataclass(frozen=True)
class PipelineConfig:
    modes: Mapping[str, LayerMode]
    """Every registered layer's mode, in canonical order."""
    allow_floor_override: bool
    sha256: str
    """Fingerprint of the effective configuration, stamped on every decision record."""
    allow_unaudited_writes: bool = False
    """A write is forwarded even when its audit record cannot be written."""
    validate_results: bool = True
    """The schema layer checks a result's structured content against the pinned output schema."""

    @property
    def enabled_layers(self) -> list[str]:
        return [name for name, mode in self.modes.items() if mode is not LayerMode.OFF]


def load_pipeline_config(path: Path, layer_order: Sequence[type[BaseLayer]]) -> PipelineConfig:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise PipelineConfigError(f"cannot read pipeline config {path}: {error}") from error
    return parse_pipeline_config(raw, layer_order)


def parse_pipeline_config(
    raw: Mapping[str, Any], layer_order: Sequence[type[BaseLayer]]
) -> PipelineConfig:
    _reject_unknown_keys(raw.keys(), _TOP_LEVEL_KEYS, "top-level")

    safety = _table(raw, "safety")
    _reject_unknown_keys(safety.keys(), _SAFETY_KEYS, "[safety]")
    allow_floor_override = safety.get("allow_floor_override", False)
    if not isinstance(allow_floor_override, bool):
        raise PipelineConfigError("[safety] allow_floor_override must be true or false")

    allow_unaudited_writes = safety.get("allow_unaudited_writes", False)
    if not isinstance(allow_unaudited_writes, bool):
        raise PipelineConfigError("[safety] allow_unaudited_writes must be true or false")
    if allow_unaudited_writes:
        logger.warning("writes are allowed without an audit record (allow_unaudited_writes)")

    schema = _table(raw, "schema")
    _reject_unknown_keys(schema.keys(), _SCHEMA_KEYS, "[schema]")
    validate_results = schema.get("validate_results", True)
    if not isinstance(validate_results, bool):
        raise PipelineConfigError("[schema] validate_results must be true or false")

    configured_modes = _table(raw, "layers")
    known_layers = {layer.name: layer for layer in layer_order}
    _reject_unknown_keys(configured_modes.keys(), known_layers.keys(), "[layers]")

    modes = {
        layer.name: _parse_mode(layer.name, configured_modes.get(layer.name, "enforce"))
        for layer in layer_order
    }
    _check_floor_layers(modes, known_layers, allow_floor_override)

    fingerprint_source = {
        "layers": modes,
        "allow_floor_override": allow_floor_override,
        "allow_unaudited_writes": allow_unaudited_writes,
        "validate_results": validate_results,
    }
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_source, sort_keys=True).encode()
    ).hexdigest()
    return PipelineConfig(
        modes=MappingProxyType(modes),
        allow_floor_override=allow_floor_override,
        sha256=fingerprint,
        allow_unaudited_writes=allow_unaudited_writes,
        validate_results=validate_results,
    )


def _check_floor_layers(
    modes: Mapping[str, LayerMode],
    known_layers: Mapping[str, type[BaseLayer]],
    allow_floor_override: bool,
) -> None:
    weakened = [
        name
        for name, mode in modes.items()
        if known_layers[name].floor and mode is not LayerMode.ENFORCE
    ]
    if not weakened:
        return
    if not allow_floor_override:
        raise PipelineConfigError(
            f"floor layers {weakened} can only be weakened with"
            " [safety] allow_floor_override = true"
        )
    if os.environ.get(LAB_FLOOR_OVERRIDE_ENV) != "yes":
        # A second, separate switch: a file that says allow_floor_override = true is not enough on
        # its own, so no one variable pointing the gateway at a lab pipeline can weaken a floor.
        raise PipelineConfigError(
            f"floor layers {weakened} can only be weakened by the red-team lab: it also needs"
            f" {LAB_FLOOR_OVERRIDE_ENV}=yes in the environment"
        )
    logger.warning("floor layers weakened by configuration: %s", weakened)


def _table(raw: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = raw.get(key, {})
    if not isinstance(value, Mapping):
        raise PipelineConfigError(f"[{key}] must be a table")
    return value


def _parse_mode(layer_name: str, value: object) -> LayerMode:
    modes_by_value = {mode.value: mode for mode in LayerMode}
    if isinstance(value, str) and value in modes_by_value:
        return modes_by_value[value]
    raise PipelineConfigError(
        f"layer {layer_name!r} has mode {value!r}; expected one of {', '.join(modes_by_value)}"
    )


def _reject_unknown_keys(keys: Iterable[str], allowed: Iterable[str], where: str) -> None:
    unknown = sorted(set(keys) - set(allowed))
    if unknown:
        raise PipelineConfigError(f"unknown {where} keys in pipeline config: {unknown}")
