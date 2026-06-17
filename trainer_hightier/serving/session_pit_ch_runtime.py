"""ClickHouse short-PIT runtime for closed-session ``sess__*`` features."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Final

import pandas as pd

from trainer_hightier.config import (
    SESSION_L0_EVENT_TIME_COLUMN,
    SESSION_L0_INGEST_CAP_SEC,
    SESSION_L0_OBSERVED_AT_COLUMN,
    SESSION_PIT_FEATURE_COLUMNS,
    DuckDbRuntimeConfig,
    HightierServingConfig,
    default_hightier_serving_config,
)
from trainer_hightier.feature_experiment.materialize_session_pit import (
    compute_session_pit_features_from_session_source,
    session_available_ts_sql,
)
from trainer_hightier.serving.ch_adapter import get_clickhouse_client

logger = logging.getLogger(__name__)

CH_SESSION_SCHEMA_REQUIRED_RAW_COLUMNS: Final[tuple[str, ...]] = (
    "session_id",
    SESSION_L0_EVENT_TIME_COLUMN,
    SESSION_L0_OBSERVED_AT_COLUMN,
    "num_games_with_wager",
    "num_bets",
    "turnover",
    "theo_win",
)

CH_SESSION_FETCHED_COLUMNS: Final[tuple[str, ...]] = (
    "session_id",
    "session_end_dtm",
    "session_available_ts",
    "num_games_with_wager",
    "num_bets",
    "turnover",
    "theo_win",
)


def ch_session_select_list(*, cap_sec: int = SESSION_L0_INGEST_CAP_SEC) -> str:
    """SELECT list mapping raw CH ``t_session`` to cleaned session PIT columns."""

    avail = session_available_ts_sql(cap_sec=cap_sec)
    return f"""
    session_id,
    {SESSION_L0_EVENT_TIME_COLUMN} AS session_end_dtm,
    {avail} AS session_available_ts,
    CAST(num_games_with_wager AS Nullable(Float64)) AS num_games_with_wager,
    CAST(num_bets AS Nullable(Float64)) AS num_bets,
    CAST(turnover AS Nullable(Float64)) AS turnover,
    CAST(theo_win AS Nullable(Float64)) AS theo_win
    """.strip()


def _require_scoring_bets_columns(bets: pd.DataFrame, *, require_prediction_visible: bool) -> None:
    """Validate staged bets input shape for session PIT CH runtime."""

    required = {"bet_id", "session_id", "wager"}
    if require_prediction_visible:
        required.add("prediction_visible_ts_cf")
    missing = required - frozenset(bets.columns)
    if missing:
        raise ValueError(
            f"session_pit CH runtime missing bet columns {sorted(missing)}; "
            f"got {list(bets.columns)!r}",
        )


def _scoring_bets_work_frame(
    bets: pd.DataFrame,
    *,
    require_prediction_visible: bool,
) -> pd.DataFrame:
    """Return minimal bet frame for DuckDB aggregation."""

    _require_scoring_bets_columns(bets, require_prediction_visible=require_prediction_visible)
    cols = ["bet_id", "session_id", "wager"]
    if require_prediction_visible:
        cols.append("prediction_visible_ts_cf")
    return bets[cols].copy()


def _session_query_window(bets: pd.DataFrame) -> tuple[datetime, datetime]:
    """Return bounded fetch window for one scoring batch."""

    if "prediction_visible_ts_cf" in bets.columns:
        ts = pd.to_datetime(bets["prediction_visible_ts_cf"], utc=True)
    else:
        ts = pd.to_datetime(bets["payout_complete_dtm"], utc=True)
    if ts.isna().any():
        raise ValueError("session_pit CH fetch: decision timestamp contains null")
    ws = ts.min().to_pydatetime() - timedelta(days=1)
    we = ts.max().to_pydatetime() + timedelta(minutes=5)
    return ws, we


def fetch_session_window_for_bets(
    bets: pd.DataFrame,
    *,
    cfg: HightierServingConfig | None = None,
    cap_sec: int = SESSION_L0_INGEST_CAP_SEC,
) -> pd.DataFrame:
    """Fetch normalized ``t_session`` rows from ClickHouse for one scoring batch."""

    if bets.empty:
        return pd.DataFrame(columns=list(CH_SESSION_FETCHED_COLUMNS))
    _require_scoring_bets_columns(bets, require_prediction_visible=False)
    serving = cfg or default_hightier_serving_config()
    client = get_clickhouse_client()
    ws, we = _session_query_window(bets)
    session_ids = sorted({int(x) for x in bets["session_id"].dropna().astype(int).unique()})
    if not session_ids:
        return pd.DataFrame(columns=list(CH_SESSION_FETCHED_COLUMNS))
    chunk_sz = int(serving.hightier_scorer_player_id_chunk_size)
    select_list = ch_session_select_list(cap_sec=cap_sec)
    table = f"{serving.source_db}.{serving.tsession}"
    frames: list[pd.DataFrame] = []
    for i in range(0, len(session_ids), chunk_sz):
        chunk = session_ids[i : i + chunk_sz]
        in_list = ",".join(str(x) for x in chunk)
        q = f"""
            SELECT {select_list}
            FROM {table} FINAL
            WHERE session_id IN ({in_list})
              AND {SESSION_L0_EVENT_TIME_COLUMN} >= %(ws)s
              AND {SESSION_L0_EVENT_TIME_COLUMN} <= %(we)s
              AND session_id IS NOT NULL
              AND {SESSION_L0_EVENT_TIME_COLUMN} IS NOT NULL
              AND {SESSION_L0_OBSERVED_AT_COLUMN} IS NOT NULL
              AND is_deleted = 0
              AND is_canceled = 0
              AND is_manual = 0
              AND (
                coalesce(turnover, 0) > 0
                OR coalesce(num_games_with_wager, 0) > 0
              )
        """
        part = client.query_df(q, parameters={"ws": ws, "we": we})
        if not part.empty:
            frames.append(part)
    if not frames:
        return pd.DataFrame(columns=list(CH_SESSION_FETCHED_COLUMNS))
    out = pd.concat(frames, ignore_index=True)
    out["session_end_dtm"] = pd.to_datetime(out["session_end_dtm"], utc=True)
    out["session_available_ts"] = pd.to_datetime(out["session_available_ts"], utc=True)
    return out


def assert_ch_session_supplier_ready_or_raise(
    *,
    cfg: HightierServingConfig | None = None,
    cap_sec: int = SESSION_L0_INGEST_CAP_SEC,
) -> dict[str, Any]:
    """Schema smoke: verify ClickHouse ``t_session`` exposes required raw fields."""

    serving = cfg or default_hightier_serving_config()
    client = get_clickhouse_client()
    table = f"{serving.source_db}.{serving.tsession}"
    select_list = ch_session_select_list(cap_sec=cap_sec)
    q = f"SELECT {select_list} FROM {table} FINAL LIMIT 1"
    try:
        sample = client.query_df(q)
    except Exception as exc:
        raise RuntimeError(
            f"[session_pit] ClickHouse schema smoke failed for {table}: {exc}",
        ) from exc
    missing = [c for c in CH_SESSION_FETCHED_COLUMNS if c not in sample.columns]
    if missing:
        raise RuntimeError(
            f"[session_pit] ClickHouse {table} missing mapped columns {missing}; "
            f"got {list(sample.columns)!r}",
        )
    return {
        "supplier": "clickhouse_session_pit",
        "table": table,
        "availability_mapping": session_available_ts_sql(cap_sec=cap_sec),
        "ingest_cap_sec": int(cap_sec),
        "sample_row_count": int(len(sample)),
        "output_columns": list(SESSION_PIT_FEATURE_COLUMNS),
    }


def compute_session_pit_features_for_bets_ch(
    bets: pd.DataFrame,
    *,
    cfg: HightierServingConfig | None = None,
    duckdb_runtime: DuckDbRuntimeConfig | None = None,
    session_rows: pd.DataFrame | None = None,
    use_pcd_availability_cutoff: bool = False,
) -> pd.DataFrame:
    """Compute bet-grain ``sess__*`` from ClickHouse ``t_session`` (production path)."""

    out_cols = list(SESSION_PIT_FEATURE_COLUMNS)
    if bets.empty:
        return pd.DataFrame(columns=["bet_id", *out_cols])
    require_pv = not use_pcd_availability_cutoff
    cols = ["bet_id", "session_id", "wager"]
    if require_pv:
        cols.append("prediction_visible_ts_cf")
    else:
        cols.append("payout_complete_dtm")
    _require_scoring_bets_columns(bets, require_prediction_visible=require_pv)
    work = bets[cols].copy()
    runtime = duckdb_runtime or DuckDbRuntimeConfig()
    fetched = session_rows if session_rows is not None else fetch_session_window_for_bets(
        bets,
        cfg=cfg,
    )
    if fetched.empty:
        fetched = pd.DataFrame(columns=list(CH_SESSION_FETCHED_COLUMNS))
    extra_select = ""
    cutoff_expr = "tr.avail_cutoff"
    if not use_pcd_availability_cutoff:
        extra_select = ",\n    CAST(prediction_visible_ts_cf AS TIMESTAMPTZ) AS avail_cutoff"
    elif "payout_complete_dtm" not in work.columns:
        raise ValueError(
            "use_pcd_availability_cutoff=True requires payout_complete_dtm on bets frame",
        )
    else:
        extra_select = ",\n    CAST(payout_complete_dtm AS TIMESTAMPTZ) AS avail_cutoff"
    return compute_session_pit_features_from_session_source(
        bets,
        session_source_read="fetched_session",
        duckdb_runtime=runtime,
        availability_cutoff_expr=cutoff_expr,
        bet_source="scoring_bets",
        scoring_bets_frame=work,
        session_frame=fetched,
        bet_rows_extra_select=extra_select,
    )
