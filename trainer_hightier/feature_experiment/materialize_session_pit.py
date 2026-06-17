"""Materialize PIT-safe closed-session ``sess__*`` features at bet grain."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Final

import duckdb
import pandas as pd

from trainer_hightier.config import (
    SESSION_L0_EVENT_TIME_COLUMN,
    SESSION_L0_INGEST_CAP_SEC,
    SESSION_L0_OBSERVED_AT_COLUMN,
    SESSION_L0_SYNTHETIC_OBSERVED_AT_COLUMN,
    SESSION_PIT_FEATURE_COLUMNS,
    SESSION_PIT_MATERIALIZER_VERSION,
    SESSION_PIT_SOURCE_CONTRACT_REF,
    DuckDbRuntimeConfig,
)
from trainer_hightier.utils.duckdb_runtime import apply_duckdb_runtime_pragmas
from trainer_hightier.utils.session_l0_preprocess import default_cleaned_session_parquet_path

logger = logging.getLogger(__name__)

_SESSION_VALID_BODY: Final[str] = """
  SELECT
    TRY_CAST(session_id AS BIGINT) AS session_id,
    CAST(session_end_dtm AS TIMESTAMPTZ) AS session_end_dtm,
    CAST({available_col} AS TIMESTAMPTZ) AS session_available_ts,
    TRY_CAST(num_games_with_wager AS DOUBLE) AS num_games_with_wager,
    TRY_CAST(num_bets AS DOUBLE) AS num_bets,
    TRY_CAST(turnover AS DOUBLE) AS turnover,
    TRY_CAST(theo_win AS DOUBLE) AS theo_win
  FROM {session_read} AS s
  WHERE session_id IS NOT NULL
    AND session_end_dtm IS NOT NULL
    AND {available_col} IS NOT NULL
    AND COALESCE(TRY_CAST(is_manual AS INTEGER), 0) = 0
    AND COALESCE(TRY_CAST(is_deleted AS INTEGER), 0) = 0
    AND COALESCE(TRY_CAST(is_canceled AS INTEGER), 0) = 0
    AND (
      COALESCE(TRY_CAST(turnover AS DOUBLE), 0) > 0
      OR COALESCE(TRY_CAST(num_games_with_wager AS DOUBLE), 0) > 0
    )
"""


def _path_esc(path: Path) -> str:
    return str(Path(path).resolve()).replace("\\", "/").replace("'", "''")


def session_available_ts_sql(
    *,
    event_col: str = SESSION_L0_EVENT_TIME_COLUMN,
    observed_col: str = SESSION_L0_OBSERVED_AT_COLUMN,
    cap_sec: int = SESSION_L0_INGEST_CAP_SEC,
    table_alias: str | None = None,
) -> str:
    """Conservative session availability timestamp (matches txn_lite CH pattern)."""

    prefix = f"{table_alias}." if table_alias else ""
    evt = f"{prefix}{event_col}"
    obs = f"{prefix}{observed_col}"
    return (
        f"greatest("
        f"least({obs}, {evt} + INTERVAL {int(cap_sec)} SECOND), "
        f"{evt})"
    )


def _session_valid_cte(session_read: str, *, use_synthetic: bool = True) -> str:
    available_col = (
        SESSION_L0_SYNTHETIC_OBSERVED_AT_COLUMN
        if use_synthetic
        else session_available_ts_sql(table_alias="s")
    )
    if use_synthetic:
        available_col = f's."{SESSION_L0_SYNTHETIC_OBSERVED_AT_COLUMN}"'
    return (
        "session_valid AS ("
        + _SESSION_VALID_BODY.format(
            session_read=session_read,
            available_col=available_col,
        )
        + ")"
    )


def _feature_select_sql(*, wager_expr: str = "tr.wager") -> str:
    return f"""
  CASE WHEN sv.session_id IS NOT NULL THEN 1 ELSE 0 END AS sess__available_flag,
  ln(1 + greatest(coalesce(sv.num_games_with_wager, 0.0), 0.0)) AS sess__num_games_with_wager_log1p,
  ln(1 + greatest(coalesce(sv.num_bets, 0.0), 0.0)) AS sess__num_bets_log1p,
  ln(1 + greatest(coalesce(sv.turnover, 0.0), 0.0)) AS sess__turnover_log1p,
  sign(coalesce(sv.theo_win, 0.0)) * ln(1 + abs(coalesce(sv.theo_win, 0.0))) AS sess__theo_win_log1p_signed,
  CASE
    WHEN coalesce(sv.turnover, 0.0) > 0 AND coalesce(sv.num_games_with_wager, 0.0) > 0
    THEN ln(1 + {wager_expr} / (sv.turnover / sv.num_games_with_wager))
    ELSE 0.0
  END AS sess__bet_wager_over_sess_avg_log1p
""".strip()


def _build_session_pit_sql(
    *,
    bet_source: str,
    session_read: str,
    cleaned_bet_read: str | None,
    availability_cutoff_expr: str,
    bet_rows_extra_select: str = "",
) -> str:
    """Build DuckDB SQL for bet-grain session PIT features."""

    bet_join = ""
    if cleaned_bet_read is not None:
        bet_join = f"""
LEFT JOIN {cleaned_bet_read} AS cb
  ON TRY_CAST(raw.bet_id AS DOUBLE) = TRY_CAST(cb.bet_id AS DOUBLE)"""
        bet_projection = (
            "CAST(COALESCE(cb.prediction_visible_ts_cf, raw.payout_complete_dtm) "
            "AS TIMESTAMPTZ) AS avail_cutoff"
        )
    elif bet_rows_extra_select.strip():
        bet_projection = bet_rows_extra_select.lstrip(",").strip()
    else:
        bet_projection = "CAST(raw.payout_complete_dtm AS TIMESTAMPTZ) AS avail_cutoff"
    extra = ""
    session_cte = _session_valid_cte(session_read, use_synthetic=True)
    return f"""
WITH {session_cte},
bet_rows AS (
  SELECT
    TRY_CAST(raw.bet_id AS DOUBLE) AS bet_id,
    TRY_CAST(raw.session_id AS BIGINT) AS session_id,
    coalesce(TRY_CAST(raw.wager AS DOUBLE), 0.0) AS wager,
    {bet_projection}{extra}
  FROM {bet_source} AS raw{bet_join}
  WHERE TRY_CAST(raw.bet_id AS DOUBLE) IS NOT NULL
    AND TRY_CAST(raw.session_id AS BIGINT) IS NOT NULL
)
SELECT
  tr.bet_id,
  {_feature_select_sql(wager_expr="tr.wager")}
FROM bet_rows AS tr
LEFT JOIN session_valid AS sv
  ON tr.session_id = sv.session_id
 AND sv.session_end_dtm <= {availability_cutoff_expr}
 AND sv.session_available_ts <= {availability_cutoff_expr}
""".strip()


def compute_session_pit_features_from_session_source(
    bets: pd.DataFrame,
    *,
    session_source_read: str,
    duckdb_runtime: DuckDbRuntimeConfig,
    availability_cutoff_expr: str = "tr.avail_cutoff",
    bet_source: str = "scoring_bets",
    scoring_bets_frame: pd.DataFrame | None = None,
    session_frame: pd.DataFrame | None = None,
    cleaned_bet_read: str | None = None,
    bet_rows_extra_select: str = "",
) -> pd.DataFrame:
    """Compute bet-grain ``sess__*`` from a DuckDB session source or in-memory frame."""

    out_cols = list(SESSION_PIT_FEATURE_COLUMNS)
    if bets.empty:
        return pd.DataFrame(columns=["bet_id", *out_cols])
    required = frozenset({"bet_id", "session_id", "wager"})
    missing = required - frozenset(bets.columns)
    if missing:
        raise ValueError(
            f"compute_session_pit_features_from_session_source missing columns "
            f"{sorted(missing)}; got {list(bets.columns)!r}",
        )
    session_read = session_source_read
    if session_frame is not None:
        session_read = "fetched_session"
    sql = _build_session_pit_sql(
        bet_source=bet_source,
        session_read=session_read,
        cleaned_bet_read=cleaned_bet_read,
        availability_cutoff_expr=availability_cutoff_expr,
        bet_rows_extra_select=bet_rows_extra_select,
    )
    work = scoring_bets_frame if scoring_bets_frame is not None else bets.copy()
    con = duckdb.connect(database=":memory:")
    try:
        apply_duckdb_runtime_pragmas(con, duckdb_runtime)
        con.register("scoring_bets", work)
        if session_frame is not None:
            con.register("fetched_session", session_frame)
        return con.execute(sql).df()
    finally:
        con.close()


def compute_session_pit_features_for_bets(
    bets: pd.DataFrame,
    *,
    cleaned_session_parquet: Path | None = None,
    cleaned_bet_read: str | None = None,
    duckdb_runtime: DuckDbRuntimeConfig,
) -> pd.DataFrame:
    """Compute bet-grain ``sess__*`` from cleaned ``t_session`` parquet."""

    session_path = (
        Path(cleaned_session_parquet).resolve()
        if cleaned_session_parquet is not None
        else default_cleaned_session_parquet_path().resolve()
    )
    if not session_path.is_file():
        raise FileNotFoundError(f"cleaned session parquet missing: {session_path}")
    session_read = f"read_parquet('{_path_esc(session_path)}')"
    required = frozenset({"bet_id", "session_id", "wager", "payout_complete_dtm"})
    missing = required - frozenset(bets.columns)
    if missing:
        raise ValueError(
            f"compute_session_pit_features_for_bets missing columns {sorted(missing)}; "
            f"got {list(bets.columns)!r}",
        )
    work = bets[list(required)].copy()
    extra_select = ""
    cleaned_read = cleaned_bet_read
    if "prediction_visible_ts_cf" in bets.columns:
        work["prediction_visible_ts_cf"] = bets["prediction_visible_ts_cf"]
        extra_select = ",\n    CAST(prediction_visible_ts_cf AS TIMESTAMPTZ) AS avail_cutoff"
        cleaned_read = None
    return compute_session_pit_features_from_session_source(
        bets,
        session_source_read=session_read,
        duckdb_runtime=duckdb_runtime,
        availability_cutoff_expr="tr.avail_cutoff",
        bet_source="scoring_bets",
        scoring_bets_frame=work,
        cleaned_bet_read=cleaned_read,
        bet_rows_extra_select=extra_select,
    )


def materialize_session_pit_parquet(
    *,
    training_parquet_for_bet_ids: Path,
    out_parquet: Path,
    duckdb_runtime: DuckDbRuntimeConfig,
    cleaned_session_parquet: Path | None = None,
    cleaned_bet_read: str | None = None,
) -> dict[str, Any]:
    """Materialize ``sess__*`` for all rows in a training/split parquet."""

    train_path = Path(training_parquet_for_bet_ids).resolve()
    out = Path(out_parquet).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    session_path = (
        Path(cleaned_session_parquet).resolve()
        if cleaned_session_parquet is not None
        else default_cleaned_session_parquet_path().resolve()
    )
    session_read = f"read_parquet('{_path_esc(session_path)}')"
    bet_source = f"read_parquet('{_path_esc(train_path)}')"
    sql = _build_session_pit_sql(
        bet_source=bet_source,
        session_read=session_read,
        cleaned_bet_read=cleaned_bet_read,
        availability_cutoff_expr="tr.avail_cutoff",
    )
    oq = _path_esc(out)
    con = duckdb.connect(database=":memory:")
    try:
        apply_duckdb_runtime_pragmas(con, duckdb_runtime)
        con.execute(f"COPY ({sql}) TO '{oq}' (FORMAT PARQUET, COMPRESSION SNAPPY)")
        train_n = int(con.execute(f"SELECT COUNT(*) FROM read_parquet('{_path_esc(train_path)}')").fetchone()[0])
        out_n = int(con.execute(f"SELECT COUNT(*) FROM read_parquet('{oq}')").fetchone()[0])
        avail_n = int(
            con.execute(
                f"SELECT COUNT(*) FROM read_parquet('{oq}') "
                f"WHERE sess__available_flag = 1",
            ).fetchone()[0],
        )
    finally:
        con.close()
    meta = {
        "source_name": "t_session",
        "source_contract_ref": SESSION_PIT_SOURCE_CONTRACT_REF,
        "materializer_code_version": SESSION_PIT_MATERIALIZER_VERSION,
        "pit_event_time": SESSION_L0_EVENT_TIME_COLUMN,
        "pit_available_time": SESSION_L0_SYNTHETIC_OBSERVED_AT_COLUMN,
        "ingest_delay_cap_sec": SESSION_L0_INGEST_CAP_SEC,
        "join_grain": "session_id x prediction_visible_ts_cf (closed session only)",
        "cleaned_session_parquet": str(session_path),
        "training_row_count": train_n,
        "materialized_bet_row_count": out_n,
        "session_available_row_count": avail_n,
        "feature_columns": list(SESSION_PIT_FEATURE_COLUMNS),
    }
    logger.info(
        "[session_pit] materialized %d bet rows (available=%d/%d) → %s",
        out_n,
        avail_n,
        out_n,
        out,
    )
    return meta


def enrich_split_parquet_with_session_pit(
    *,
    split_parquet: Path,
    session_pit_parquet: Path,
    out_parquet: Path,
    duckdb_runtime: DuckDbRuntimeConfig,
) -> Path:
    """Left-join materialized ``sess__*`` columns onto one split parquet."""

    split = _path_esc(Path(split_parquet))
    sess = _path_esc(Path(session_pit_parquet))
    out = Path(out_parquet).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    cols = ",\n  ".join(f's."{c}" AS "{c}"' for c in SESSION_PIT_FEATURE_COLUMNS)
    sql = f"""
SELECT
  b.*,
  {cols}
FROM read_parquet('{split}') AS b
LEFT JOIN read_parquet('{sess}') AS s
  ON TRY_CAST(b.bet_id AS DOUBLE) = s.bet_id
"""
    con = duckdb.connect(database=":memory:")
    try:
        apply_duckdb_runtime_pragmas(con, duckdb_runtime)
        con.execute(f"COPY ({sql}) TO '{_path_esc(out)}' (FORMAT PARQUET, COMPRESSION SNAPPY)")
    finally:
        con.close()
    return out


def write_session_pit_sidecars(
    *,
    run_dir: Path,
    materialization_meta: dict[str, Any],
    out_parquet: Path,
) -> Path:
    """Write ``external_sources/t_session/materialization_report.json``."""

    root = Path(run_dir).resolve() / "external_sources" / "t_session"
    root.mkdir(parents=True, exist_ok=True)
    report_path = root / "materialization_report.json"
    payload = {
        **materialization_meta,
        "materialized_features_path": str(Path(out_parquet).resolve()),
    }
    report_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return report_path
