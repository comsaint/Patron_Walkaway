"""Flight recorder configuration (YAML in bundle; no env-driven behavior)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

FLIGHT_RECORDER_SCHEMA_VERSION: str = "flight_recorder_v1"
DEFAULT_RECORDING_ROOT_REL: str = "local_state/flight_recording"
DEFAULT_CONFIG_REL: str = "local_state/flight_recording_config.yaml"


@dataclass
class FlightRecorderConfig:
    """Recording behavior for production flight recorder."""

    enabled: bool = True
    recording_root: str = DEFAULT_RECORDING_ROOT_REL
    capture_scorer_stages: bool = True
    capture_validator_stages: bool = True
    capture_ch_diagnostic_requery: bool = True
    requery_schedule_minutes: tuple[int, ...] = (0, 15, 60, 360, 1440, 4320)
    include_non_final_diagnostics: bool = True
    include_system_table_probes: bool = True
    include_full_population_diagnostic: bool = False
    include_allowlist_exact_production_path: bool = True
    source_context_lookback_hours: int = 6
    source_context_lookahead_hours: int = 72
    parquet_compression: str = "zstd"
    parquet_row_group_size: int = 65536
    max_scorer_cycles: int | None = None
    max_validator_cycles: int | None = None
    max_recording_duration_hours: float | None = None
    redact_hostnames: bool = True
    redact_connection_strings: bool = True

    def resolve_recording_root(self, bundle_root: Path) -> Path:
        """Return absolute recording root under *bundle_root*."""
        p = Path(self.recording_root)
        if p.is_absolute():
            return p.resolve()
        return (bundle_root / p).resolve()

    @classmethod
    def from_mapping(cls, data: dict[str, Any] | None) -> FlightRecorderConfig:
        """Build config from a YAML/JSON mapping."""
        if not data:
            return cls()
        schedule = data.get("requery_schedule_minutes", (0, 15, 60, 360, 1440, 4320))
        if isinstance(schedule, list):
            schedule_tuple = tuple(int(x) for x in schedule)
        else:
            schedule_tuple = cls().requery_schedule_minutes
        return cls(
            enabled=bool(data.get("enabled", True)),
            recording_root=str(data.get("recording_root", DEFAULT_RECORDING_ROOT_REL)),
            capture_scorer_stages=bool(data.get("capture_scorer_stages", True)),
            capture_validator_stages=bool(data.get("capture_validator_stages", True)),
            capture_ch_diagnostic_requery=bool(data.get("capture_ch_diagnostic_requery", True)),
            requery_schedule_minutes=schedule_tuple,
            include_non_final_diagnostics=bool(data.get("include_non_final_diagnostics", True)),
            include_system_table_probes=bool(data.get("include_system_table_probes", True)),
            include_full_population_diagnostic=bool(
                data.get("include_full_population_diagnostic", False)
            ),
            include_allowlist_exact_production_path=bool(
                data.get("include_allowlist_exact_production_path", True)
            ),
            source_context_lookback_hours=int(data.get("source_context_lookback_hours", 6)),
            source_context_lookahead_hours=int(data.get("source_context_lookahead_hours", 72)),
            parquet_compression=str(data.get("parquet_compression", "zstd")),
            parquet_row_group_size=int(data.get("parquet_row_group_size", 65536)),
            max_scorer_cycles=_optional_int(data.get("max_scorer_cycles")),
            max_validator_cycles=_optional_int(data.get("max_validator_cycles")),
            max_recording_duration_hours=_optional_float(
                data.get("max_recording_duration_hours")
            ),
            redact_hostnames=bool(data.get("redact_hostnames", True)),
            redact_connection_strings=bool(data.get("redact_connection_strings", True)),
        )

    @classmethod
    def from_yaml_path(cls, path: Path) -> FlightRecorderConfig:
        """Load config from a YAML file."""
        if not path.is_file():
            raise FileNotFoundError(f"recording config not found: {path}")
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if raw is not None and not isinstance(raw, dict):
            raise ValueError(
                f"recording config must be a mapping, got {type(raw).__name__}"
            )
        return cls.from_mapping(raw if isinstance(raw, dict) else None)

    def to_mapping(self) -> dict[str, Any]:
        """Serialize config to a JSON/YAML-safe mapping (no secrets)."""
        return {
            "enabled": self.enabled,
            "recording_root": self.recording_root,
            "capture_scorer_stages": self.capture_scorer_stages,
            "capture_validator_stages": self.capture_validator_stages,
            "capture_ch_diagnostic_requery": self.capture_ch_diagnostic_requery,
            "requery_schedule_minutes": list(self.requery_schedule_minutes),
            "include_non_final_diagnostics": self.include_non_final_diagnostics,
            "include_system_table_probes": self.include_system_table_probes,
            "include_full_population_diagnostic": self.include_full_population_diagnostic,
            "include_allowlist_exact_production_path": (
                self.include_allowlist_exact_production_path
            ),
            "source_context_lookback_hours": self.source_context_lookback_hours,
            "source_context_lookahead_hours": self.source_context_lookahead_hours,
            "parquet_compression": self.parquet_compression,
            "parquet_row_group_size": self.parquet_row_group_size,
            "max_scorer_cycles": self.max_scorer_cycles,
            "max_validator_cycles": self.max_validator_cycles,
            "max_recording_duration_hours": self.max_recording_duration_hours,
            "redact_hostnames": self.redact_hostnames,
            "redact_connection_strings": self.redact_connection_strings,
        }

    def write_yaml(self, path: Path) -> None:
        """Write config as YAML (for bundle-local template)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.safe_dump(self.to_mapping(), sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )


def _optional_int(value: Any) -> int | None:
    """Parse optional int from config value."""
    if value is None:
        return None
    return int(value)


def _optional_float(value: Any) -> float | None:
    """Parse optional float from config value."""
    if value is None:
        return None
    return float(value)


def default_config_for_bundle(bundle_root: Path) -> FlightRecorderConfig:
    """Load bundle config YAML or return defaults."""
    cfg_path = bundle_root / DEFAULT_CONFIG_REL
    if cfg_path.is_file():
        return FlightRecorderConfig.from_yaml_path(cfg_path)
    return FlightRecorderConfig()
