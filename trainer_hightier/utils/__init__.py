"""Non-pipeline helpers for ``trainer_hightier`` (shared utilities)."""

from __future__ import annotations

from trainer_hightier.utils.canonical_mapping import (
    build_canonical_mapping_from_cleaned_session_parquet,
    default_canonical_mapping_artifacts_dir,
    default_canonical_mapping_parquet_path,
    default_canonical_mapping_sidecar_path,
)
from trainer_hightier.utils.patron_session_metrics import (
    compile_canonical_patron_session_metrics,
    default_patron_session_metrics_parquet_path,
)
from trainer_hightier.utils.duckdb_runtime import (
    apply_duckdb_runtime_pragmas,
    execute_query_df_with_progress,
    execute_sql_with_progress,
)

__all__ = [
    "apply_duckdb_runtime_pragmas",
    "build_canonical_mapping_from_cleaned_session_parquet",
    "compile_canonical_patron_session_metrics",
    "default_canonical_mapping_artifacts_dir",
    "default_canonical_mapping_parquet_path",
    "default_canonical_mapping_sidecar_path",
    "default_patron_session_metrics_parquet_path",
    "execute_query_df_with_progress",
    "execute_sql_with_progress",
]
