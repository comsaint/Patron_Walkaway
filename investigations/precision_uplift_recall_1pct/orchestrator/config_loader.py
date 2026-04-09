"""Load and validate Phase 1 orchestrator YAML config."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import yaml

REQUIRED_ROOT_KEYS: tuple[str, ...] = (
    "model_dir",
    "state_db_path",
    "prediction_log_db_path",
    "window",
    "thresholds",
)
REQUIRED_WINDOW_KEYS: tuple[str, ...] = ("start_ts", "end_ts")
REQUIRED_THRESHOLD_KEYS: tuple[str, ...] = (
    "min_hours_preliminary",
    "min_finalized_alerts_preliminary",
    "min_finalized_true_positives_preliminary",
    "min_hours_gate",
    "min_finalized_alerts_gate",
    "min_finalized_true_positives_gate",
)


class ConfigValidationError(ValueError):
    """Raised when config YAML is missing required fields or has wrong types."""

    def __init__(self, message: str) -> None:
        super().__init__(f"E_CONFIG_INVALID: {message}")


def load_raw_config(path: Path) -> dict[str, Any]:
    """Parse YAML config file into a dict.

    Args:
        path: Path to YAML file.

    Returns:
        Parsed mapping.

    Raises:
        ConfigValidationError: If file is missing or YAML is invalid.
        OSError: If the file cannot be read.
    """
    if not path.is_file():
        raise ConfigValidationError(f"config file not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigValidationError(f"invalid YAML: {exc}") from exc
    if raw is None or not isinstance(raw, dict):
        raise ConfigValidationError(
            f"config must be a mapping at root, got {type(raw).__name__}"
        )
    return raw


def validate_phase1_config(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Validate required Phase 1 fields and return the same mapping as a plain dict.

    Args:
        raw: Parsed YAML root mapping.

    Returns:
        Shallow-validated config dict.

    Raises:
        ConfigValidationError: On missing keys or wrong nested types.
    """
    missing = [k for k in REQUIRED_ROOT_KEYS if k not in raw]
    if missing:
        raise ConfigValidationError(
            f"missing keys {missing}; required {list(REQUIRED_ROOT_KEYS)}"
        )

    window = raw["window"]
    if not isinstance(window, Mapping):
        raise ConfigValidationError(
            f"window must be a mapping, got {type(window).__name__}"
        )
    w_missing = [k for k in REQUIRED_WINDOW_KEYS if k not in window]
    if w_missing:
        raise ConfigValidationError(
            f"window missing {w_missing}; required {list(REQUIRED_WINDOW_KEYS)}"
        )

    thresholds = raw["thresholds"]
    if not isinstance(thresholds, Mapping):
        raise ConfigValidationError(
            f"thresholds must be a mapping, got {type(thresholds).__name__}"
        )
    t_missing = [k for k in REQUIRED_THRESHOLD_KEYS if k not in thresholds]
    if t_missing:
        raise ConfigValidationError(
            f"thresholds missing {t_missing}; required {list(REQUIRED_THRESHOLD_KEYS)}"
        )

    return dict(raw)


def load_phase1_config(path: Path) -> dict[str, Any]:
    """Load YAML from path and validate Phase 1 schema.

    Args:
        path: Config file path.

    Returns:
        Validated config dictionary.
    """
    return validate_phase1_config(load_raw_config(path))
