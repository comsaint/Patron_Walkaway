"""Materialize 1-hour patron-centric bet behavior features for Feast trial.

Windows aggregate prior bets in the last hour, partitioned by ``canonical_id``
(aligned with walkaway labels). Requires ``canonical_player_mapping`` Parquet.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pyarrow.parquet as pq

from trainer_hightier.config import DuckDbRuntimeConfig
from trainer_hightier.utils.canonical_mapping import default_canonical_mapping_parquet_path
from trainer_hightier.utils.bet_l0_preprocess import (
    cleaned_bet_dataset_has_any_parquet,
    first_parquet_under_for_schema,
    resolved_cleaned_bet_read_parquet_sql,
)
from trainer_hightier.utils.duckdb_runtime import apply_duckdb_runtime_pragmas


def default_trial_bet_behavior_1h_parquet_path(*, repo_root: Path | None = None) -> Path:
    """Default output path: ``trainer_hightier/artifacts/feast/trial_bet_behavior_1h.parquet``."""
    base_repo = Path(__file__).resolve().parents[2] if repo_root is None else repo_root
    out_dir = base_repo / "trainer_hightier" / "artifacts" / "feast"
    return (out_dir / "trial_bet_behavior_1h.parquet").resolve()


def default_cleaned_bet_parquet_path(*, repo_root: Path | None = None) -> Path:
    """Default cleaned bet input (same default as Feast ``definitions.py``)."""
    base_repo = Path(__file__).resolve().parents[2] if repo_root is None else repo_root
    return (base_repo / "trainer_hightier" / "artifacts" / "cleaned" / "cleaned__gmwds_t_bet").resolve()


def _path_posix(path: Path) -> str:
    return str(Path(path).resolve()).replace("\\", "/")


def materialize_trial_bet_behavior_1h(
    cleaned_bet_parquet: Path | None = None,
    out_parquet: Path | None = None,
    duckdb_runtime: DuckDbRuntimeConfig | None = None,
    canonical_mapping_parquet: Path | None = None,
) -> Path:
    """Write trial Parquet: entity ``bet_id``, PIT timestamps, four 1h window features.

    Requires cleaned bet columns: ``bet_id``, ``player_id``, ``payout_complete_dtm``,
    ``wager``, ``is_back_bet``, ``payout_odds``, ``prediction_visible_ts_cf``,
    ``__etl_insert_Dtm_synthetic``.

    Prior-hour aggregates use ``canonical_id`` from ``canonical_mapping_parquet``
    (default: :func:`~trainer_hightier.utils.canonical_mapping.default_canonical_mapping_parquet_path`),
    with ``COALESCE(mapping, CAST(player_id AS VARCHAR))`` when a row has no mapping row.

    Args:
        cleaned_bet_parquet: Input cleaned bet path; default under ``trainer_hightier/artifacts/cleaned/``.
        out_parquet: Output path; default ``artifacts/feast/trial_bet_behavior_1h.parquet``.
        duckdb_runtime: Optional DuckDB PRAGMAs.
        canonical_mapping_parquet: ``player_id`` / ``canonical_id`` Parquet; uses package default if omitted.

    Returns:
        Resolved path to the written Parquet file.

    Raises:
        FileNotFoundError: If the input Parquet does not exist or canonical mapping is missing.
        ValueError: If required columns are missing (see error message).
    """
    src = Path(cleaned_bet_parquet or default_cleaned_bet_parquet_path()).resolve()
    if not (src.is_file() or cleaned_bet_dataset_has_any_parquet(src)):
        raise FileNotFoundError(f"cleaned bet parquet not found: {src}")
    dst = Path(out_parquet or default_trial_bet_behavior_1h_parquet_path()).resolve()
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmap_path = (
        Path(canonical_mapping_parquet).resolve()
        if canonical_mapping_parquet is not None
        else default_canonical_mapping_parquet_path().resolve()
    )
    if not cmap_path.is_file():
        raise FileNotFoundError(
            f"canonical_player_mapping parquet missing: {cmap_path}; "
            "run trainer_hightier.utils.canonical_mapping materialization first."
        )

    need = (
        "bet_id",
        "player_id",
        "payout_complete_dtm",
        "wager",
        "is_back_bet",
        "payout_odds",
        "prediction_visible_ts_cf",
        "__etl_insert_Dtm_synthetic",
    )
    cols = set(pq.read_schema(first_parquet_under_for_schema(src)).names)
    missing = tuple(c for c in need if c not in cols)
    if missing:
        raise ValueError(f"cleaned bet missing columns {list(missing)}; got {sorted(cols)}")

    bet_from = resolved_cleaned_bet_read_parquet_sql(src)
    dst_esc = _path_posix(dst).replace("'", "''")
    cmap_esc = _path_posix(cmap_path).replace("'", "''")
    inner = f"""
WITH cmap AS (
  SELECT DISTINCT
    TRY_CAST(player_id AS BIGINT) AS player_id,
    TRIM(CAST(canonical_id AS VARCHAR)) AS canonical_id
  FROM read_parquet('{cmap_esc}')
  WHERE TRY_CAST(player_id AS BIGINT) IS NOT NULL
    AND TRIM(CAST(canonical_id AS VARCHAR)) <> ''
),
src AS (
  SELECT
    TRY_CAST(_bet_in."bet_id" AS DOUBLE) AS "bet_id",
    TRY_CAST(_bet_in."player_id" AS BIGINT) AS "player_id",
    COALESCE(
      c.canonical_id,
      CAST(TRY_CAST(_bet_in."player_id" AS BIGINT) AS VARCHAR)
    ) AS "canonical_id",
    CAST(_bet_in."payout_complete_dtm" AS TIMESTAMPTZ) AS "pcd",
    TRY_CAST(_bet_in."wager" AS DOUBLE) AS "wager",
    TRY_CAST(_bet_in."is_back_bet" AS INTEGER) AS "is_back_bet",
    TRY_CAST(_bet_in."payout_odds" AS DOUBLE) AS "payout_odds",
    CAST(_bet_in."prediction_visible_ts_cf" AS TIMESTAMPTZ) AS "prediction_visible_ts_cf",
    CAST(_bet_in."__etl_insert_Dtm_synthetic" AS TIMESTAMPTZ) AS "__etl_insert_Dtm_synthetic"
  FROM {bet_from} AS _bet_in
  LEFT JOIN cmap AS c ON TRY_CAST(_bet_in."player_id" AS BIGINT) = c.player_id
  WHERE TRY_CAST(_bet_in."player_id" AS BIGINT) IS NOT NULL
    AND TRY_CAST(_bet_in."bet_id" AS DOUBLE) IS NOT NULL
    AND _bet_in."payout_complete_dtm" IS NOT NULL
),
ordered AS (
  SELECT
    *,
    COUNT(*) OVER (
      PARTITION BY "canonical_id"
      ORDER BY "pcd"
      RANGE BETWEEN INTERVAL '1' HOUR PRECEDING AND INTERVAL '1' MICROSECOND PRECEDING
    )::BIGINT AS "_cnt",
    COALESCE(
      SUM("wager") OVER (
        PARTITION BY "canonical_id"
        ORDER BY "pcd"
        RANGE BETWEEN INTERVAL '1' HOUR PRECEDING AND INTERVAL '1' MICROSECOND PRECEDING
      ),
      0.0
    ) AS "_wsum",
    COALESCE(
      SUM(CASE WHEN COALESCE("is_back_bet", 0) = 1 THEN 1.0 ELSE 0.0 END) OVER (
        PARTITION BY "canonical_id"
        ORDER BY "pcd"
        RANGE BETWEEN INTERVAL '1' HOUR PRECEDING AND INTERVAL '1' MICROSECOND PRECEDING
      ),
      0.0
    ) AS "_back_sum",
    AVG("payout_odds") OVER (
      PARTITION BY "canonical_id"
      ORDER BY "pcd"
      RANGE BETWEEN INTERVAL '1' HOUR PRECEDING AND INTERVAL '1' MICROSECOND PRECEDING
    ) AS "_odds_avg"
  FROM src
)
SELECT
  "bet_id",
  "prediction_visible_ts_cf",
  "__etl_insert_Dtm_synthetic",
  "_cnt" AS "bet__bets_cnt__w1h",
  CAST("_wsum" AS DOUBLE) AS "bet__wager_sum__w1h",
  CAST(
    CASE WHEN "_cnt" > 0 THEN "_back_sum" / CAST("_cnt" AS DOUBLE) ELSE 0.0 END
  AS DOUBLE) AS "bet__back_bet_ratio__w1h",
  CAST(COALESCE("_odds_avg", 0.0) AS DOUBLE) AS "bet__payout_odds_avg__w1h"
FROM ordered
""".strip()

    con = duckdb.connect(database=":memory:")
    try:
        if duckdb_runtime is not None:
            apply_duckdb_runtime_pragmas(con, duckdb_runtime)
        con.execute(f"COPY ({inner}) TO '{dst_esc}' (FORMAT PARQUET, COMPRESSION SNAPPY)")
    finally:
        con.close()
    return dst


def _main() -> None:
    """CLI entrypoint for manual Feast offline trial Parquet (not used on the training path)."""

    import argparse

    parser = argparse.ArgumentParser(
        description="Materialize full-history trial 1h Parquet for Feast FileSource / diagnostics.",
    )
    parser.add_argument(
        "--cleaned-bet-parquet",
        type=Path,
        default=None,
        help="Cleaned bet hive root (default: trainer_hightier artifacts cleaned bet).",
    )
    parser.add_argument(
        "--out-parquet",
        type=Path,
        default=None,
        help="Output path (default: artifacts/feast/trial_bet_behavior_1h.parquet).",
    )
    ns = parser.parse_args()
    out = materialize_trial_bet_behavior_1h(
        cleaned_bet_parquet=ns.cleaned_bet_parquet,
        out_parquet=ns.out_parquet,
    )
    print(out.resolve())


if __name__ == "__main__":
    _main()
