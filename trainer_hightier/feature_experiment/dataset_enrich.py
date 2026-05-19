"""Join baseline Step-3 training Parquet with ``fe__*`` DuckDB-derived columns."""

from __future__ import annotations

from pathlib import Path

import duckdb

from trainer_hightier.config import (
    DuckDbRuntimeConfig,
    MID_TERM_ANCHOR_AUDIT_COLUMN,
    MID_TERM_SNAPSHOT_AGE_AUDIT_COLUMN,
    MID_TERM_SNAPSHOT_MISSING_AUDIT_COLUMN,
)
import trainer_hightier.feature_experiment.feature_registry as _feature_registry
from trainer_hightier.utils.duckdb_runtime import apply_duckdb_runtime_pragmas

_MID_TERM_DERIVED_EXPRS: dict[str, str] = {
    "fe__bets_cnt__w1d": "CAST(b._snap_bets_cnt_w1d AS DOUBLE)",
    "fe__wager_sum__w1d": "CAST(b._snap_wager_sum_w1d AS DOUBLE)",
    "fe__wager_sum__w15m_over_w1d": """
CASE
  WHEN b._snap_wager_sum_w1d IS NOT NULL AND b._snap_wager_sum_w1d > 1e-9 AND s."fe__wager_sum__w15m" IS NOT NULL
  THEN CAST(s."fe__wager_sum__w15m" / b._snap_wager_sum_w1d AS DOUBLE)
  ELSE CAST(NULL AS DOUBLE)
END""".strip(),
    "fe__wager_cv_w7d": """
CASE
  WHEN b._snap_avg_abs_wager_w7d IS NOT NULL AND b._snap_avg_abs_wager_w7d > 1e-12 AND b._snap_std_wager_w7d IS NOT NULL
  THEN CAST(b._snap_std_wager_w7d / b._snap_avg_abs_wager_w7d AS DOUBLE)
  ELSE CAST(NULL AS DOUBLE)
END""".strip(),
    "fe__payout_odds_z_prior_w30d": """
CASE
  WHEN b._snap_prior_odds_std_w30d IS NOT NULL AND ABS(b._snap_prior_odds_std_w30d) > 1e-12
   AND b._snap_prior_odds_mean_w30d IS NOT NULL AND TRY_CAST(b.payout_odds AS DOUBLE) IS NOT NULL
  THEN CAST((TRY_CAST(b.payout_odds AS DOUBLE) - b._snap_prior_odds_mean_w30d) / b._snap_prior_odds_std_w30d AS DOUBLE)
  ELSE CAST(NULL AS DOUBLE)
END""".strip(),
    "fe__interarrival__last_gap_z__w7d": """
CASE
  WHEN b._snap_interarrival_std_w7d IS NOT NULL AND b._snap_interarrival_std_w7d > 1e-9
   AND b._snap_interarrival_avg_w7d IS NOT NULL AND s."fe__time_since_last_bet_sec" IS NOT NULL
  THEN CAST((s."fe__time_since_last_bet_sec" - b._snap_interarrival_avg_w7d) / b._snap_interarrival_std_w7d AS DOUBLE)
  ELSE CAST(NULL AS DOUBLE)
END""".strip(),
}

_MID_TERM_STAGING_COLUMNS: tuple[str, ...] = (
    "_cid",
    "_gday",
    "_snap_bets_cnt_w1d",
    "_snap_wager_sum_w1d",
    "_snap_prior_odds_mean_w30d",
    "_snap_prior_odds_std_w30d",
    "_snap_std_wager_w7d",
    "_snap_avg_abs_wager_w7d",
    "_snap_interarrival_avg_w7d",
    "_snap_interarrival_std_w7d",
)


def _esc(p: Path) -> str:
    return str(Path(p).resolve()).replace("\\", "/").replace("'", "''")


def enrich_training_parquet(
    *,
    base_training_parquet: Path,
    fe_derived_parquet: Path,
    out_parquet: Path,
    duckdb_runtime: DuckDbRuntimeConfig,
) -> Path:
    """Legacy left-join ``fe__*`` aggregates onto Step-3 training parquet (by ``bet_id``)."""

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


def enrich_training_parquet_with_cadence_suppliers(
    *,
    base_training_parquet: Path,
    fe_short_term_parquet: Path | None,
    mid_term_snapshot_parquet: Path | None,
    out_parquet: Path,
    duckdb_runtime: DuckDbRuntimeConfig,
    short_term_columns: tuple[str, ...],
    mid_term_columns: tuple[str, ...],
    include_audit_columns: bool = True,
) -> Path:
    """Join short-term bet-grain features and mid-term daily snapshot ASOF features."""

    if not short_term_columns and not mid_term_columns:
        raise ValueError("enrich requires at least one short-term or mid-term fe__ column")
    composite_mid = {"fe__wager_sum__w15m_over_w1d", "fe__interarrival__last_gap_z__w7d"}
    if composite_mid.intersection(mid_term_columns) and not short_term_columns:
        raise ValueError(
            f"mid-term composite columns {sorted(composite_mid.intersection(mid_term_columns))} "
            "require short-term dependency columns",
        )
    bq = _esc(base_training_parquet)
    out = Path(out_parquet).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    oq = _esc(out)

    short_select = ""
    short_join = ""
    if short_term_columns and fe_short_term_parquet is not None:
        fq = _esc(fe_short_term_parquet)
        short_select = ",\n  ".join(f's."{c}" AS "{c}"' for c in short_term_columns)
        short_join = f"""
LEFT JOIN read_parquet('{fq}') AS s
  ON TRY_CAST(b.bet_id AS DOUBLE) = s.bet_id"""

    mid_cte = ""
    mid_select = ""
    audit_select = ""
    exclude_cols = ""
    if mid_term_columns and mid_term_snapshot_parquet is not None:
        mq = _esc(mid_term_snapshot_parquet)
        mid_cte = f"""
mid_snap AS (
  SELECT * FROM read_parquet('{mq}')
),
b_with_day AS (
  SELECT
    b.*,
    TRIM(CAST(b.canonical_id AS VARCHAR)) AS _cid,
    CAST(b.gaming_day AS DATE) AS _gday
  FROM read_parquet('{bq}') AS b
),
mid_asof AS (
  SELECT
    bw.*,
    CAST(lst.anchor_gaming_day AS DATE) AS {MID_TERM_ANCHOR_AUDIT_COLUMN},
    CASE
      WHEN lst.anchor_gaming_day IS NULL OR bw._gday IS NULL THEN CAST(NULL AS BIGINT)
      ELSE DATE_DIFF('day', CAST(lst.anchor_gaming_day AS DATE), bw._gday)
    END AS {MID_TERM_SNAPSHOT_AGE_AUDIT_COLUMN},
    CASE WHEN lst.anchor_gaming_day IS NULL THEN 1 ELSE 0 END AS {MID_TERM_SNAPSHOT_MISSING_AUDIT_COLUMN},
    lst.fe__bets_cnt__w1d AS _snap_bets_cnt_w1d,
    lst.fe__wager_sum__w1d AS _snap_wager_sum_w1d,
    lst.fe__prior_odds_mean_w30d AS _snap_prior_odds_mean_w30d,
    lst.fe__prior_odds_std_w30d AS _snap_prior_odds_std_w30d,
    lst.fe__std_wager_w7d AS _snap_std_wager_w7d,
    lst.fe__avg_abs_wager_w7d AS _snap_avg_abs_wager_w7d,
    lst.fe__interarrival_avg_w7d AS _snap_interarrival_avg_w7d,
    lst.fe__interarrival_std_w7d AS _snap_interarrival_std_w7d
  FROM b_with_day AS bw
  LEFT JOIN LATERAL (
    SELECT *
    FROM mid_snap AS s
    WHERE TRIM(CAST(s.canonical_id AS VARCHAR)) = bw._cid
      AND CAST(s.anchor_gaming_day AS DATE) < bw._gday
    ORDER BY CAST(s.anchor_gaming_day AS DATE) DESC
    LIMIT 1
  ) AS lst ON TRUE
)"""
        mid_parts: list[str] = []
        for col in mid_term_columns:
            expr = _MID_TERM_DERIVED_EXPRS.get(col, f'CAST(b."{col}" AS DOUBLE)')
            mid_parts.append(f'{expr} AS "{col}"')
        mid_select = ",\n  ".join(mid_parts)
        if include_audit_columns:
            audit_select = f""",
  b.{MID_TERM_ANCHOR_AUDIT_COLUMN},
  b.{MID_TERM_SNAPSHOT_AGE_AUDIT_COLUMN},
  b.{MID_TERM_SNAPSHOT_MISSING_AUDIT_COLUMN}"""
        exclude_cols = ", ".join(_MID_TERM_STAGING_COLUMNS)

    if mid_cte:
        base_from = "FROM mid_asof AS b"
        base_select = f"b.* EXCLUDE ({exclude_cols})" if exclude_cols else "b.*"
    else:
        base_from = f"FROM read_parquet('{bq}') AS b"
        base_select = "b.*"

    select_parts = [base_select]
    if short_select:
        select_parts.append(short_select)
    if mid_select:
        select_parts.append(mid_select)
    if audit_select:
        select_parts.append(audit_select.strip(",\n"))

    if mid_cte:
        inner = f"""
WITH {mid_cte.strip()}
SELECT
  {", ".join(select_parts)}
{base_from}
{short_join}
""".strip()
    else:
        inner = f"""
SELECT
  {", ".join(select_parts)}
{base_from}
{short_join}
""".strip()

    con = duckdb.connect(database=":memory:")
    try:
        apply_duckdb_runtime_pragmas(con, duckdb_runtime)
        con.execute(f"COPY ({inner}) TO '{oq}' (FORMAT PARQUET, COMPRESSION SNAPPY)")
    finally:
        con.close()
    return out
