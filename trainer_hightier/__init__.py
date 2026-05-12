"""Simplified high-tier patron training / reporting (skeleton)."""

from __future__ import annotations

from trainer_hightier.config import (
    CanonicalMappingConfig,
    DuckDbRuntimeConfig,
    HighTierObjectiveConfig,
    SessionPreprocessConfig,
)
from trainer_hightier.utils.duckdb_runtime import (
    apply_duckdb_runtime_pragmas,
    execute_query_df_with_progress,
    execute_sql_with_progress,
)
from trainer_hightier.eval import PrecisionFloorReport, report_alert_rate_at_precision_floor

__all__ = [
    "apply_duckdb_runtime_pragmas",
    "CanonicalMappingConfig",
    "execute_query_df_with_progress",
    "execute_sql_with_progress",
    "DuckDbRuntimeConfig",
    "HighTierObjectiveConfig",
    "SessionPreprocessConfig",
    "PrecisionFloorReport",
    "report_alert_rate_at_precision_floor",
]
