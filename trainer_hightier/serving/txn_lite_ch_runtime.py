"""ClickHouse short-PIT runtime for production ``txn__*`` features."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pandas as pd

from trainer_hightier.config import (
    TXN_LITE_FEATURE_COLUMNS,
    TXN_L0_EVENT_TIME_COLUMN,
    TXN_L0_INGEST_CAP_SEC,
    TXN_L0_OBSERVED_AT_COLUMN,
    DuckDbRuntimeConfig,
    HightierServingConfig,
    default_hightier_serving_config,
    txn_lite_feature_columns,
)
from trainer_hightier.feature_experiment.materialize_txn_lite import (
    compute_txn_lite_features_for_bets,
    compute_txn_lite_features_from_txn_source,
)
from trainer_hightier.serving.ch_adapter import get_clickhouse_client

logger = logging.getLogger(__name__)

CH_TXN_SCHEMA_REQUIRED_RAW_COLUMNS: Final[tuple[str, ...]] = (
    "player_id",
    TXN_L0_EVENT_TIME_COLUMN,
    TXN_L0_OBSERVED_AT_COLUMN,
    "type",
    "sub_type",
    "txn_value",
    "action",
    "status",
    "buyin_status",
)

CH_TXN_FETCHED_COLUMNS: Final[tuple[str, ...]] = (
    "player_id",
    "txn_event_ts",
    "txn_available_ts",
    "type",
    "sub_type",
    "txn_value",
    "action",
    "status",
    "buyin_status",
)


def ch_txn_available_ts_sql(*, cap_sec: int = TXN_L0_INGEST_CAP_SEC) -> str:
    """Conservative ClickHouse availability timestamp (no silent event-time fallback)."""

    evt = TXN_L0_EVENT_TIME_COLUMN
    obs = TXN_L0_OBSERVED_AT_COLUMN
    return (
        f"greatest("
        f"least({obs}, {evt} + INTERVAL {int(cap_sec)} SECOND), "
        f"{evt})"
    )


def ch_txn_select_list(*, cap_sec: int = TXN_L0_INGEST_CAP_SEC) -> str:
    """SELECT list mapping raw CH ``t_casino_txn`` to cleaned L1 column names."""

    avail = ch_txn_available_ts_sql(cap_sec=cap_sec)
    return f"""
    player_id,
    {TXN_L0_EVENT_TIME_COLUMN} AS txn_event_ts,
    {avail} AS txn_available_ts,
    type,
    sub_type,
    CAST(txn_value AS Nullable(Float64)) AS txn_value,
    action,
    status,
    buyin_status
    """.strip()


def _require_scoring_bets_columns(bets: pd.DataFrame, *, require_prediction_visible: bool) -> None:
    """Validate staged bets input shape for txn_lite CH runtime."""

    required = {"bet_id", "player_id", "payout_complete_dtm"}
    if require_prediction_visible:
        required.add("prediction_visible_ts_cf")
    missing = required - frozenset(bets.columns)
    if missing:
        raise ValueError(
            f"txn_lite CH runtime missing bet columns {sorted(missing)}; "
            f"got {list(bets.columns)!r}",
        )


def _scoring_bets_work_frame(
    bets: pd.DataFrame,
    *,
    require_prediction_visible: bool,
) -> pd.DataFrame:
    """Return minimal bet frame for DuckDB aggregation."""

    _require_scoring_bets_columns(bets, require_prediction_visible=require_prediction_visible)
    cols = ["bet_id", "player_id", "payout_complete_dtm"]
    if require_prediction_visible:
        cols.append("prediction_visible_ts_cf")
    return bets[cols].copy()


def _txn_query_window(bets: pd.DataFrame) -> tuple[datetime, datetime]:
    """Return bounded ``start_dtm`` fetch window for one scoring batch."""

    pcd = pd.to_datetime(bets["payout_complete_dtm"], utc=True)
    if pcd.isna().any():
        raise ValueError("txn_lite CH fetch: payout_complete_dtm contains null")
    min_pcd = pcd.min().to_pydatetime()
    max_pcd = pcd.max().to_pydatetime()
    ws = min_pcd - timedelta(hours=1, minutes=5)
    we = max_pcd + timedelta(minutes=1)
    return ws, we


def fetch_casino_txn_window_for_bets(
    bets: pd.DataFrame,
    *,
    cfg: HightierServingConfig | None = None,
    cap_sec: int = TXN_L0_INGEST_CAP_SEC,
) -> pd.DataFrame:
    """Fetch normalized ``t_casino_txn`` rows from ClickHouse for one scoring batch."""

    if bets.empty:
        return pd.DataFrame(columns=list(CH_TXN_FETCHED_COLUMNS))
    _require_scoring_bets_columns(bets, require_prediction_visible=False)
    serving = cfg or default_hightier_serving_config()
    client = get_clickhouse_client()
    ws, we = _txn_query_window(bets)
    player_ids = sorted({int(x) for x in bets["player_id"].dropna().astype(int).unique()})
    if not player_ids:
        return pd.DataFrame(columns=list(CH_TXN_FETCHED_COLUMNS))
    chunk_sz = int(serving.hightier_scorer_player_id_chunk_size)
    select_list = ch_txn_select_list(cap_sec=cap_sec)
    table = f"{serving.source_db}.{serving.tcasino_txn}"
    frames: list[pd.DataFrame] = []
    for i in range(0, len(player_ids), chunk_sz):
        chunk = player_ids[i : i + chunk_sz]
        in_list = ",".join(str(x) for x in chunk)
        q = f"""
            SELECT {select_list}
            FROM {table} FINAL
            WHERE player_id IN ({in_list})
              AND {TXN_L0_EVENT_TIME_COLUMN} >= %(ws)s
              AND {TXN_L0_EVENT_TIME_COLUMN} < %(we)s
              AND player_id IS NOT NULL
              AND {TXN_L0_EVENT_TIME_COLUMN} IS NOT NULL
              AND {TXN_L0_OBSERVED_AT_COLUMN} IS NOT NULL
        """
        part = client.query_df(q, parameters={"ws": ws, "we": we})
        if not part.empty:
            frames.append(part)
    if not frames:
        return pd.DataFrame(columns=list(CH_TXN_FETCHED_COLUMNS))
    out = pd.concat(frames, ignore_index=True)
    for col in ("txn_event_ts", "txn_available_ts"):
        out[col] = pd.to_datetime(out[col], utc=True)
    return out


def assert_ch_txn_supplier_ready_or_raise(
    *,
    cfg: HightierServingConfig | None = None,
    cap_sec: int = TXN_L0_INGEST_CAP_SEC,
) -> dict[str, Any]:
    """Schema smoke: verify ClickHouse ``t_casino_txn`` exposes required raw fields."""

    serving = cfg or default_hightier_serving_config()
    client = get_clickhouse_client()
    table = f"{serving.source_db}.{serving.tcasino_txn}"
    select_list = ch_txn_select_list(cap_sec=cap_sec)
    q = f"SELECT {select_list} FROM {table} FINAL LIMIT 1"
    try:
        sample = client.query_df(q)
    except Exception as exc:
        raise RuntimeError(
            f"[txn_lite] ClickHouse schema smoke failed for {table}: {exc}",
        ) from exc
    missing = [c for c in CH_TXN_FETCHED_COLUMNS if c not in sample.columns]
    if missing:
        raise RuntimeError(
            f"[txn_lite] ClickHouse {table} missing mapped columns {missing}; "
            f"got {list(sample.columns)!r}",
        )
    return {
        "supplier": "clickhouse_short_pit",
        "table": table,
        "availability_mapping": ch_txn_available_ts_sql(cap_sec=cap_sec),
        "ingest_cap_sec": int(cap_sec),
        "sample_row_count": int(len(sample)),
        "output_columns": list(CH_TXN_FETCHED_COLUMNS),
    }


def compute_txn_lite_features_for_bets_ch(
    bets: pd.DataFrame,
    *,
    cfg: HightierServingConfig | None = None,
    duckdb_runtime: DuckDbRuntimeConfig | None = None,
    extra_window_hours: tuple[int, ...] = (),
    txn_rows: pd.DataFrame | None = None,
    use_pcd_availability_cutoff: bool = False,
) -> pd.DataFrame:
    """Compute bet-grain ``txn__*`` from ClickHouse ``t_casino_txn`` (production path)."""

    out_feature_cols = txn_lite_feature_columns(extra_window_hours=extra_window_hours)
    if bets.empty:
        return pd.DataFrame(columns=["bet_id", *out_feature_cols])
    require_pv = not use_pcd_availability_cutoff
    work = _scoring_bets_work_frame(bets, require_prediction_visible=require_pv)
    runtime = duckdb_runtime or DuckDbRuntimeConfig()
    fetched = txn_rows if txn_rows is not None else fetch_casino_txn_window_for_bets(
        bets,
        cfg=cfg,
    )
    if fetched.empty:
        fetched = pd.DataFrame(columns=list(CH_TXN_FETCHED_COLUMNS))
    extra_select = ""
    cutoff_expr = "tr.pcd"
    if not use_pcd_availability_cutoff:
        extra_select = ",\n    CAST(prediction_visible_ts_cf AS TIMESTAMPTZ) AS avail_cutoff"
        cutoff_expr = "tr.avail_cutoff"
    return compute_txn_lite_features_from_txn_source(
        bets,
        txn_source_read="fetched_txn",
        duckdb_runtime=runtime,
        extra_window_hours=extra_window_hours,
        availability_cutoff_expr=cutoff_expr,
        train_rows_extra_select=extra_select,
        scoring_bets_frame=work,
        txn_frame=fetched,
    )


def run_txn_lite_parity_gate(
    bets: pd.DataFrame,
    *,
    cleaned_casino_txn_root: Path,
    cfg: HightierServingConfig | None = None,
    duckdb_runtime: DuckDbRuntimeConfig | None = None,
    txn_rows: pd.DataFrame | None = None,
    hard_fail_fraction: float = 0.005,
    warn_fraction: float = 0.02,
) -> dict[str, Any]:
    """Compare cleaned parquet vs CH-runtime ``txn__*`` on the same bet sample."""

    feature_cols = list(TXN_LITE_FEATURE_COLUMNS)
    if bets.empty:
        return {
            "sample_size": 0,
            "verdict": "pass",
            "column_diff_fractions": {},
            "max_diff_fraction": 0.0,
        }
    runtime = duckdb_runtime or DuckDbRuntimeConfig()
    cleaned = compute_txn_lite_features_for_bets(
        bets,
        cleaned_casino_txn_root=cleaned_casino_txn_root,
        duckdb_runtime=runtime,
    )
    ch_path = compute_txn_lite_features_for_bets_ch(
        bets,
        cfg=cfg,
        duckdb_runtime=runtime,
        txn_rows=txn_rows,
        use_pcd_availability_cutoff=True,
    )
    merged = cleaned.merge(
        ch_path,
        on="bet_id",
        how="outer",
        suffixes=("_cleaned", "_ch"),
        indicator=True,
    )
    n = int(len(bets))
    col_fracs: dict[str, float] = {}
    for col in feature_cols:
        left = merged[f"{col}_cleaned"]
        right = merged[f"{col}_ch"]
        both = left.notna() & right.notna()
        if int(both.sum()) == 0:
            diff_n = int((left.notna() | right.notna()).sum())
        else:
            diff_n = int((left[both] != right[both]).sum())
            diff_n += int((left.isna() ^ right.isna()).sum())
        col_fracs[col] = float(diff_n / n) if n else 0.0
    max_frac = max(col_fracs.values()) if col_fracs else 0.0
    verdict = "pass"
    if max_frac > hard_fail_fraction:
        verdict = "fail"
    elif max_frac > warn_fraction:
        verdict = "warn"
    return {
        "sample_size": n,
        "verdict": verdict,
        "column_diff_fractions": col_fracs,
        "max_diff_fraction": max_frac,
        "hard_fail_fraction": hard_fail_fraction,
        "warn_fraction": warn_fraction,
        "merge_only_cleaned": int((merged["_merge"] == "left_only").sum()),
        "merge_only_ch": int((merged["_merge"] == "right_only").sum()),
    }
