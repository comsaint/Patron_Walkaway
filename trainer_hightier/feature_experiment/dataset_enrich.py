"""Join Step-3 training Parquet with cadence suppliers (short PIT cache, mid snapshot ASOF).

Short-term columns come from an **offline PIT cache** parquet (legacy basename
``_main_trainer_fe_short_term.parquet``; manifest key ``fe_short_term_parquet``)—per-row
PIT values for training ``bet_id`` only, not a reusable global feature table.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import duckdb

from trainer_hightier.config import (
    DuckDbRuntimeConfig,
    HK_TZ,
    MID_TERM_ANCHOR_AUDIT_COLUMN,
    MID_TERM_SNAPSHOT_AGE_AUDIT_COLUMN,
    MID_TERM_SNAPSHOT_MISSING_AUDIT_COLUMN,
    PRODUCTION_MID_ASOF_BACKFILL_DAYS,
)
from trainer_hightier.serving.mid_term_bounded_asof import (
    mid_asof_lateral_lower_bound_sql,
    mid_snapshot_missing_flag_sql,
    resolve_mid_asof_backfill_days,
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
    "fe__odds__payout_odds_z__w7d": """
CASE
  WHEN b._snap_payout_odds_std_w7d IS NOT NULL AND ABS(b._snap_payout_odds_std_w7d) > 1e-12
   AND b._snap_payout_odds_avg_w7d IS NOT NULL AND TRY_CAST(b.payout_odds AS DOUBLE) IS NOT NULL
  THEN CAST((TRY_CAST(b.payout_odds AS DOUBLE) - b._snap_payout_odds_avg_w7d) / b._snap_payout_odds_std_w7d AS DOUBLE)
  ELSE CAST(NULL AS DOUBLE)
END""".strip(),
    "fe__clock__day_of_week": f"""
CAST(
  EXTRACT(
    dow FROM CAST(b.payout_complete_dtm AS TIMESTAMPTZ) AT TIME ZONE '{HK_TZ}'
  ) AS DOUBLE
)""".strip(),
    "fe__clock__is_weekend": f"""
CASE
  WHEN EXTRACT(
    dow FROM CAST(b.payout_complete_dtm AS TIMESTAMPTZ) AT TIME ZONE '{HK_TZ}'
  ) IN (0, 6) THEN 1.0
  ELSE 0.0
END""".strip(),
}

_MID_TERM_STAGING_SQL: dict[str, str] = {
    "_snap_bets_cnt_w1d": "lst.fe__bets_cnt__w1d AS _snap_bets_cnt_w1d",
    "_snap_wager_sum_w1d": "lst.fe__wager_sum__w1d AS _snap_wager_sum_w1d",
    "_snap_prior_odds_mean_w30d": "lst.fe__prior_odds_mean_w30d AS _snap_prior_odds_mean_w30d",
    "_snap_prior_odds_std_w30d": "lst.fe__prior_odds_std_w30d AS _snap_prior_odds_std_w30d",
    "_snap_std_wager_w7d": "lst.fe__std_wager_w7d AS _snap_std_wager_w7d",
    "_snap_avg_abs_wager_w7d": "lst.fe__avg_abs_wager_w7d AS _snap_avg_abs_wager_w7d",
    "_snap_interarrival_avg_w7d": "lst.fe__interarrival_avg_w7d AS _snap_interarrival_avg_w7d",
    "_snap_interarrival_std_w7d": "lst.fe__interarrival_std_w7d AS _snap_interarrival_std_w7d",
    "_snap_payout_odds_avg_w7d": "lst.fe__payout_odds_avg_w7d AS _snap_payout_odds_avg_w7d",
    "_snap_payout_odds_std_w7d": "lst.fe__payout_odds_std_w7d AS _snap_payout_odds_std_w7d",
}

_MID_TERM_COL_TO_STAGING: dict[str, tuple[str, ...]] = {
    "fe__bets_cnt__w1d": ("_snap_bets_cnt_w1d",),
    "fe__wager_sum__w1d": ("_snap_wager_sum_w1d",),
    "fe__std_wager_w7d": ("_snap_std_wager_w7d",),
    "fe__avg_abs_wager_w7d": ("_snap_avg_abs_wager_w7d",),
    "fe__prior_odds_mean_w30d": ("_snap_prior_odds_mean_w30d",),
    "fe__prior_odds_std_w30d": ("_snap_prior_odds_std_w30d",),
    "fe__interarrival_avg_w7d": ("_snap_interarrival_avg_w7d",),
    "fe__interarrival_std_w7d": ("_snap_interarrival_std_w7d",),
    "fe__payout_odds_avg_w7d": ("_snap_payout_odds_avg_w7d",),
    "fe__payout_odds_std_w7d": ("_snap_payout_odds_std_w7d",),
    "fe__wager_sum__w15m_over_w1d": ("_snap_wager_sum_w1d",),
    "fe__wager_cv_w7d": ("_snap_std_wager_w7d", "_snap_avg_abs_wager_w7d"),
    "fe__payout_odds_z_prior_w30d": ("_snap_prior_odds_mean_w30d", "_snap_prior_odds_std_w30d"),
    "fe__interarrival__last_gap_z__w7d": ("_snap_interarrival_avg_w7d", "_snap_interarrival_std_w7d"),
    "fe__odds__payout_odds_z__w7d": ("_snap_payout_odds_avg_w7d", "_snap_payout_odds_std_w7d"),
}


def _mid_term_staging_aliases(mid_term_columns: tuple[str, ...]) -> tuple[str, ...]:
    """Return snapshot staging aliases required by requested mid-term output columns."""

    needed: list[str] = []
    for col in mid_term_columns:
        needed.extend(_MID_TERM_COL_TO_STAGING.get(col, ()))
    return tuple(dict.fromkeys(needed))


def _esc(p: Path) -> str:
    return str(Path(p).resolve()).replace("\\", "/").replace("'", "''")


def enrich_training_parquet(
    *,
    base_training_parquet: Path,
    fe_derived_parquet: Path,
    out_parquet: Path,
    duckdb_runtime: DuckDbRuntimeConfig,
    txn_lite_parquet: Path | None = None,
    txn_feature_columns: Sequence[str] | None = None,
) -> Path:
    """Legacy left-join ``fe__*`` aggregates onto Step-3 training parquet (by ``bet_id``)."""

    bq = _esc(base_training_parquet)
    fq = _esc(fe_derived_parquet)
    out = Path(out_parquet).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    oq = _esc(out)
    experimental_cols = list(_feature_registry.EXPERIMENTAL_NUMERIC_COLUMNS)
    fe_only = [c for c in experimental_cols if c.startswith("fe__")]
    fe_cols = ", ".join(f'd."{c}" AS "{c}"' for c in fe_only)
    txn_join = ""
    txn_cols = ""
    if txn_lite_parquet is not None:
        tq = _esc(txn_lite_parquet)
        from trainer_hightier.config import TXN_LITE_FEATURE_COLUMNS

        txn_col_list = (
            list(txn_feature_columns)
            if txn_feature_columns is not None
            else list(TXN_LITE_FEATURE_COLUMNS)
        )
        txn_cols = ",\n  " + ",\n  ".join(f't."{c}" AS "{c}"' for c in txn_col_list)
        txn_join = f"""
LEFT JOIN read_parquet('{tq}') AS t
  ON TRY_CAST(b.bet_id AS DOUBLE) = t.bet_id"""
    elif any(c.startswith("txn__") for c in experimental_cols):
        raise ValueError(
            "Registry includes txn__ candidate columns but txn_lite_parquet was not provided; "
            "enable external_sources.t_casino_txn in experiment_config.yaml",
        )
    inner = f"""
SELECT
  b.*,
  {fe_cols}{txn_cols}
FROM read_parquet('{bq}') AS b
LEFT JOIN read_parquet('{fq}') AS d
  ON TRY_CAST(b.bet_id AS DOUBLE) = d.bet_id{txn_join}
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
    mid_asof_backfill_days: int | None = None,
) -> Path:
    """Join short-term bet-grain features and mid-term daily snapshot ASOF features."""

    if not short_term_columns and not mid_term_columns:
        raise ValueError("enrich requires at least one short-term or mid-term fe__ column")
    backfill_n = (
        resolve_mid_asof_backfill_days(mid_asof_backfill_days)
        if mid_term_columns
        else PRODUCTION_MID_ASOF_BACKFILL_DAYS
    )
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
        staging_aliases = _mid_term_staging_aliases(mid_term_columns)
        staging_select = ",\n    ".join(_MID_TERM_STAGING_SQL[a] for a in staging_aliases)
        if staging_select:
            staging_select = f",\n    {staging_select}"
        lateral_lb = mid_asof_lateral_lower_bound_sql(
            "bw._gday", n_days=backfill_n, anchor_alias="mid_row"
        )
        missing_expr = mid_snapshot_missing_flag_sql(
            "lst.anchor_gaming_day_event",
            "bw._gday",
            n_days=backfill_n,
        )
        mid_cte = f"""
mid_snap AS (
  SELECT * FROM read_parquet('{mq}')
),
b_with_day AS (
  SELECT
    b.*,
    TRIM(CAST(b.canonical_id AS VARCHAR)) AS _cid,
    CAST(b.gaming_day_event AS DATE) AS _gday
  FROM read_parquet('{bq}') AS b
),
mid_asof AS (
  SELECT
    bw.*,
    CAST(lst.anchor_gaming_day_event AS DATE) AS {MID_TERM_ANCHOR_AUDIT_COLUMN},
    CASE
      WHEN lst.anchor_gaming_day_event IS NULL OR bw._gday IS NULL THEN CAST(NULL AS BIGINT)
      ELSE DATE_DIFF('day', CAST(lst.anchor_gaming_day_event AS DATE), bw._gday)
    END AS {MID_TERM_SNAPSHOT_AGE_AUDIT_COLUMN},
    {missing_expr} AS {MID_TERM_SNAPSHOT_MISSING_AUDIT_COLUMN}{staging_select}
  FROM b_with_day AS bw
  LEFT JOIN LATERAL (
    SELECT *
    FROM mid_snap AS mid_row
    WHERE TRIM(CAST(mid_row.canonical_id AS VARCHAR)) = bw._cid
      AND CAST(mid_row.anchor_gaming_day_event AS DATE) < bw._gday
      {lateral_lb}
    ORDER BY CAST(mid_row.anchor_gaming_day_event AS DATE) DESC
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
        exclude_cols = ", ".join(("_cid", "_gday", *staging_aliases))

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


def join_txn_lite_onto_parquet(
    *,
    base_parquet: Path,
    txn_lite_parquet: Path,
    out_parquet: Path,
    duckdb_runtime: DuckDbRuntimeConfig,
    txn_feature_columns: Sequence[str] | None = None,
) -> Path:
    """Left-join bet-grain ``txn__*`` columns onto a training parquet by ``bet_id``."""

    from trainer_hightier.config import TXN_LITE_FEATURE_COLUMNS

    bq = _esc(base_parquet)
    tq = _esc(txn_lite_parquet)
    out = Path(out_parquet).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    oq = _esc(out)
    txn_col_list = (
        list(txn_feature_columns)
        if txn_feature_columns is not None
        else list(TXN_LITE_FEATURE_COLUMNS)
    )
    txn_select = ",\n  ".join(f't."{c}" AS "{c}"' for c in txn_col_list)
    inner = f"""
SELECT
  b.*,
  {txn_select}
FROM read_parquet('{bq}') AS b
LEFT JOIN read_parquet('{tq}') AS t
  ON TRY_CAST(b.bet_id AS DOUBLE) = t.bet_id
""".strip()
    con = duckdb.connect(database=":memory:")
    try:
        apply_duckdb_runtime_pragmas(con, duckdb_runtime)
        con.execute(f"COPY ({inner}) TO '{oq}' (FORMAT PARQUET, COMPRESSION SNAPPY)")
    finally:
        con.close()
    return out
