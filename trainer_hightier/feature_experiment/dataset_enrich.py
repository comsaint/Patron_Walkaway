"""Join baseline Step-3 training Parquet with ``fe__*`` DuckDB-derived columns."""

from __future__ import annotations

from pathlib import Path

import duckdb

from trainer_hightier.config import DuckDbRuntimeConfig
import trainer_hightier.feature_experiment.feature_registry as _feature_registry
from trainer_hightier.utils.duckdb_runtime import apply_duckdb_runtime_pragmas


def _esc(p: Path) -> str:
    return str(Path(p).resolve()).replace("\\", "/").replace("'", "''")


def enrich_training_parquet(
    *,
    base_training_parquet: Path,
    fe_derived_parquet: Path,
    out_parquet: Path,
    duckdb_runtime: DuckDbRuntimeConfig,
) -> Path:
    """Left-join ``fe__*`` aggregates onto the Step-3 training parquet (by ``bet_id``)."""

    bq = _esc(base_training_parquet)
    fq = _esc(fe_derived_parquet)
    out = Path(out_parquet).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    oq = _esc(out)
    experimental_cols = list(_feature_registry.EXPERIMENTAL_NUMERIC_COLUMNS)
    fe_cols = ", ".join(f'd."{c}" AS "{c}"' for c in experimental_cols)
    inner = f"""
SELECT
  b.*,
  {fe_cols}
FROM read_parquet('{bq}') AS b
LEFT JOIN read_parquet('{fq}') AS d
  ON TRY_CAST(b.bet_id AS DOUBLE) = d.bet_id
""".strip()
    con = duckdb.connect(database=":memory:")
    try:
        apply_duckdb_runtime_pragmas(con, duckdb_runtime)
        con.execute(f"COPY ({inner}) TO '{oq}' (FORMAT PARQUET, COMPRESSION SNAPPY)")
    finally:
        con.close()
    return out
