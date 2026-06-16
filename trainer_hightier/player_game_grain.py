"""Player-game grain materialization, observation-time DQ, and serving holdback helpers."""

from __future__ import annotations

import importlib
import json
import logging
import pickle
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import duckdb
import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from trainer_hightier.config import (
    DuckDbRuntimeConfig,
    HighTierObjectiveConfig,
    Step5TrainConfig,
    SCORER_POLL_INTERVAL_SECONDS,
)
from trainer_hightier.evaluation.metrics_blocks import split_metrics_block
from trainer_hightier.evaluation.player_alert_policy import (
    ALERT_TS_COLUMN,
    LABEL_COLUMN as PG_LABEL_COLUMN,
    SCORE_COLUMN,
    TIE_BREAK_COLUMN,
    build_player_alert_policy_metadata,
    operational_simulated_metrics_block,
)
from trainer_hightier.reporting.writer import slim_training_metrics_body
from trainer_hightier.utils.bet_l0_preprocess import (
    default_cleaned_bet_parquet_path,
    resolved_cleaned_bet_read_parquet_sql,
)
from trainer_hightier.utils.duckdb_runtime import apply_duckdb_runtime_pragmas

logger = logging.getLogger(__name__)

PLAYER_ID_COLUMN: Final[str] = "player_id"
GAME_ID_COLUMN: Final[str] = "game_id"
BET_ID_COLUMN: Final[str] = "bet_id"
LABEL_COLUMN: Final[str] = "walkaway_label"
PCD_COLUMN: Final[str] = "payout_complete_dtm"
PV_COLUMN: Final[str] = "prediction_visible_ts_cf"
SYN_COLUMN: Final[str] = "__etl_insert_Dtm_synthetic"
TYPE_OF_BET_COLUMN: Final[str] = "type_of_bet"
WAGER_COLUMN: Final[str] = "wager"

PLAYER_GAME_READY_TS_COLUMN: Final[str] = "player_game_ready_ts"
PLAYER_GAME_PCD_COLUMN: Final[str] = "player_game_payout_complete_dtm"
PLAYER_GAME_LABEL_COLUMN: Final[str] = "player_game_label"

GROUP_KEY_COLUMNS: Final[tuple[str, ...]] = (PLAYER_ID_COLUMN, GAME_ID_COLUMN)

REQUIRED_BET_COLUMNS: Final[frozenset[str]] = frozenset(
    {
        PLAYER_ID_COLUMN,
        GAME_ID_COLUMN,
        BET_ID_COLUMN,
        LABEL_COLUMN,
        PCD_COLUMN,
        PV_COLUMN,
        TYPE_OF_BET_COLUMN,
        WAGER_COLUMN,
    },
)

PCD_SPAN_MAX_SECONDS: Final[int] = 60

PLAYER_GAME_COMPOSITION_FEATURES: Final[tuple[str, ...]] = (
    "player_game_bet_count",
    "pg__main_bet_count",
    "pg__side_bet_count",
    "pg__wager_sum",
    "pg__wager_max",
    "pg__main_wager_sum",
    "pg__side_wager_sum",
    "pg__side_to_main_wager_ratio",
    "pg__has_main_bet",
    "pg__has_side_bet",
    "pg__side_only_flag",
    "pg__pcd_span_seconds",
    "pg__prediction_visible_span_seconds",
)

PLAYER_GAME_SETTLEMENT_FEATURES: Final[tuple[str, ...]] = ("pg__casino_win_sum",)

MATERIALIZE_BET_READ_COLUMNS: Final[tuple[str, ...]] = tuple(
    sorted(
        REQUIRED_BET_COLUMNS
        | {SYN_COLUMN, "casino_win", "table_id", "session_id"},
    ),
)

SPLIT_NAMES: Final[tuple[str, ...]] = ("train", "val", "test")

MODEL_GRAIN_PLAYER_GAME: Final[str] = "player_game"
SCORE_AGGREGATION_NATIVE: Final[str] = "native"
DEFAULT_MODEL_FILENAME: Final[str] = "model.pkl"
DEFAULT_METRICS_FILENAME: Final[str] = "training_metrics.json"

_b5 = importlib.import_module("trainer_hightier.05_lgbm_train")


@dataclass(frozen=True)
class PlayerGameMaterializeAudit:
    """Counts from bet rows -> player-game materialization."""

    input_bet_rows: int
    output_player_games: int
    excluded_bet_rows: int
    excluded_player_games: int
    dq_pcd_span_violations: int
    dq_pv_span_violations: int
    dq_table_mismatch: int
    dq_session_mismatch: int


@dataclass(frozen=True)
class PlayerGameSplitMaterializeResult:
    """Paths and audit counts from bet splits -> player-game parquets."""

    out_dir: Path
    audits: dict[str, PlayerGameMaterializeAudit]


@dataclass(frozen=True)
class PlayerGameTrainResult:
    """Paths and metrics from native player-game LightGBM training."""

    model_path: Path
    metrics_path: Path
    report: dict[str, Any]
    threshold: float


def _coerce_group_id_series(series: pd.Series) -> pd.Series:
    """Coerce identifier columns to nullable ``Int64`` for stable grouping."""

    if pd.api.types.is_integer_dtype(series.dtype):
        return series.astype("Int64")
    return pd.to_numeric(series, errors="coerce").astype("Int64")


def _require_bet_columns(df: pd.DataFrame) -> None:
    """Validate input frame has required bet-level columns."""

    missing = sorted(REQUIRED_BET_COLUMNS - set(df.columns))
    if missing:
        raise ValueError(
            f"aggregate_bets_to_player_game_rows missing columns {missing!r}; "
            f"got {list(df.columns)!r}",
        )


def _group_span_seconds(ts: pd.Series) -> float:
    """Return max-min span in seconds for one timestamp series."""

    parsed = pd.to_datetime(ts, errors="coerce", utc=True)
    if parsed.notna().sum() == 0:
        return 0.0
    return float((parsed.max() - parsed.min()).total_seconds())


def _is_main_bet(series: pd.Series) -> pd.Series:
    """Return boolean mask for main bets."""

    return series.astype(str).str.upper().eq("MAIN_BET")


def _is_side_bet(series: pd.Series) -> pd.Series:
    """Return boolean mask for side bets."""

    return series.astype(str).str.upper().eq("SIDE_BET")


def compute_serving_due_ts(
    first_seen_prediction_visible: pd.Timestamp,
    *,
    holdback_seconds: int = SCORER_POLL_INTERVAL_SECONDS,
) -> pd.Timestamp:
    """Return earliest player-game score due time after visibility holdback."""

    base = pd.to_datetime(first_seen_prediction_visible, errors="coerce", utc=True)
    if pd.isna(base):
        raise ValueError(
            f"compute_serving_due_ts got null first_seen_prediction_visible={first_seen_prediction_visible!r}",
        )
    return base + pd.Timedelta(seconds=int(holdback_seconds))


def summarize_player_game_dq(
    df: pd.DataFrame,
    *,
    pv_span_max_seconds: int = SCORER_POLL_INTERVAL_SECONDS,
    pcd_span_max_seconds: int = PCD_SPAN_MAX_SECONDS,
) -> pd.DataFrame:
    """Summarize observation-time DQ metrics per ``player_id + game_id``."""

    _require_bet_columns(df)
    work = df.copy()
    work[PLAYER_ID_COLUMN] = _coerce_group_id_series(work[PLAYER_ID_COLUMN])
    work[GAME_ID_COLUMN] = _coerce_group_id_series(work[GAME_ID_COLUMN])
    valid_keys = work[PLAYER_ID_COLUMN].notna() & work[GAME_ID_COLUMN].notna()
    work = work.loc[valid_keys].copy()
    if work.empty:
        return pd.DataFrame(
            columns=[
                *GROUP_KEY_COLUMNS,
                "bet_rows",
                "pcd_span_seconds",
                "pv_span_seconds",
                "syn_span_seconds",
                "n_distinct_pcd",
                "n_distinct_pv",
                "table_id_mismatch",
                "session_id_mismatch",
                "dq_exclude",
                "dq_exclude_reason",
            ],
        )

    work["_wager"] = pd.to_numeric(work[WAGER_COLUMN], errors="coerce").fillna(0.0)
    grouped = work.groupby(list(GROUP_KEY_COLUMNS), dropna=True, sort=False)
    agg_spec: dict[str, tuple[str, object]] = {
        "bet_rows": (BET_ID_COLUMN, "count"),
        "n_distinct_pcd": (PCD_COLUMN, "nunique"),
        "n_distinct_pv": (PV_COLUMN, "nunique"),
        "pcd_span_seconds": (PCD_COLUMN, _group_span_seconds),
        "pv_span_seconds": (PV_COLUMN, _group_span_seconds),
    }
    if SYN_COLUMN in work.columns:
        agg_spec["syn_span_seconds"] = (SYN_COLUMN, _group_span_seconds)
    summary = grouped.agg(**agg_spec).reset_index()
    if "syn_span_seconds" not in summary.columns:
        summary["syn_span_seconds"] = 0.0

    if "table_id" in work.columns:
        table_mismatch = grouped["table_id"].nunique(dropna=True).reset_index(name="table_id_mismatch")
        summary = summary.merge(table_mismatch, on=list(GROUP_KEY_COLUMNS), how="left")
        summary["table_id_mismatch"] = summary["table_id_mismatch"] > 1
    else:
        summary["table_id_mismatch"] = False

    if "session_id" in work.columns:
        session_mismatch = grouped["session_id"].nunique(dropna=True).reset_index(
            name="session_id_mismatch",
        )
        summary = summary.merge(session_mismatch, on=list(GROUP_KEY_COLUMNS), how="left")
        summary["session_id_mismatch"] = summary["session_id_mismatch"] > 1
    else:
        summary["session_id_mismatch"] = False

    reasons: list[str | None] = []
    exclude_flags: list[bool] = []
    for row in summary.itertuples(index=False):
        row_reasons: list[str] = []
        if float(row.pcd_span_seconds) > float(pcd_span_max_seconds):
            row_reasons.append("pcd_span")
        if float(row.pv_span_seconds) > float(pv_span_max_seconds):
            row_reasons.append("pv_span")
        if bool(row.table_id_mismatch):
            row_reasons.append("table_mismatch")
        if bool(row.session_id_mismatch):
            row_reasons.append("session_mismatch")
        exclude_flags.append(bool(row_reasons))
        reasons.append("|".join(row_reasons) if row_reasons else None)

    summary["dq_exclude"] = exclude_flags
    summary["dq_exclude_reason"] = reasons
    return summary


def _aggregate_player_game_features(work: pd.DataFrame) -> pd.DataFrame:
    """Aggregate bet rows to one player-game row per group."""

    work = work.copy()
    work["_wager"] = pd.to_numeric(work[WAGER_COLUMN], errors="coerce").fillna(0.0)
    work["_is_main"] = _is_main_bet(work[TYPE_OF_BET_COLUMN])
    work["_is_side"] = _is_side_bet(work[TYPE_OF_BET_COLUMN])
    work["_main_wager"] = np.where(work["_is_main"], work["_wager"], 0.0)
    work["_side_wager"] = np.where(work["_is_side"], work["_wager"], 0.0)
    if "casino_win" in work.columns:
        work["_casino_win"] = pd.to_numeric(work["casino_win"], errors="coerce").fillna(0.0)
    work["_bet_id_sort"] = pd.to_numeric(work[BET_ID_COLUMN], errors="coerce").fillna(-1)
    work = work.sort_values(
        by=[*GROUP_KEY_COLUMNS, PV_COLUMN, PCD_COLUMN, "_bet_id_sort"],
        ascending=[True, True, True, True, True],
        kind="mergesort",
    )

    grouped = work.groupby(list(GROUP_KEY_COLUMNS), dropna=True, sort=False)
    agg_spec: dict[str, tuple[str, object]] = {
        "player_game_bet_count": (BET_ID_COLUMN, "count"),
        "pg__main_bet_count": ("_is_main", "sum"),
        "pg__side_bet_count": ("_is_side", "sum"),
        "pg__wager_sum": ("_wager", "sum"),
        "pg__wager_max": ("_wager", "max"),
        "pg__main_wager_sum": ("_main_wager", "sum"),
        "pg__side_wager_sum": ("_side_wager", "sum"),
        "player_game_label": (LABEL_COLUMN, "max"),
        "player_game_payout_complete_dtm": (PCD_COLUMN, "min"),
        "player_game_ready_ts": (PV_COLUMN, "max"),
        "pg__pcd_span_seconds": (PCD_COLUMN, _group_span_seconds),
        "pg__prediction_visible_span_seconds": (PV_COLUMN, _group_span_seconds),
        "representative_bet_id": (BET_ID_COLUMN, "last"),
    }
    if "_casino_win" in work.columns:
        agg_spec["pg__casino_win_sum"] = ("_casino_win", "sum")
    agg = grouped.agg(**agg_spec).reset_index()

    agg["pg__has_main_bet"] = agg["pg__main_bet_count"] > 0
    agg["pg__has_side_bet"] = agg["pg__side_bet_count"] > 0
    agg["pg__side_only_flag"] = agg["pg__has_side_bet"] & ~agg["pg__has_main_bet"]
    main_sum = agg["pg__main_wager_sum"].replace(0.0, np.nan)
    agg["pg__side_to_main_wager_ratio"] = agg["pg__side_wager_sum"] / main_sum
    agg["pg__side_to_main_wager_ratio"] = agg["pg__side_to_main_wager_ratio"].fillna(0.0)
    return agg.rename(
        columns={
            "player_game_payout_complete_dtm": PLAYER_GAME_PCD_COLUMN,
            "player_game_ready_ts": PLAYER_GAME_READY_TS_COLUMN,
            "player_game_label": PLAYER_GAME_LABEL_COLUMN,
        },
    )


def aggregate_bets_to_player_game_rows(
    df: pd.DataFrame,
    *,
    exclude_dq_violations: bool = True,
    pv_span_max_seconds: int = SCORER_POLL_INTERVAL_SECONDS,
    pcd_span_max_seconds: int = PCD_SPAN_MAX_SECONDS,
) -> tuple[pd.DataFrame, PlayerGameMaterializeAudit]:
    """Materialize one player-game row per ``player_id + game_id`` from bet rows."""

    _require_bet_columns(df)
    input_rows = int(len(df))
    work = df.copy()
    work[PLAYER_ID_COLUMN] = _coerce_group_id_series(work[PLAYER_ID_COLUMN])
    work[GAME_ID_COLUMN] = _coerce_group_id_series(work[GAME_ID_COLUMN])
    valid = (
        work[PLAYER_ID_COLUMN].notna()
        & work[GAME_ID_COLUMN].notna()
        & pd.to_numeric(work[LABEL_COLUMN], errors="coerce").notna()
        & pd.to_datetime(work[PCD_COLUMN], errors="coerce").notna()
        & pd.to_datetime(work[PV_COLUMN], errors="coerce").notna()
    )
    excluded_bets = int((~valid).sum())
    if excluded_bets > 0:
        logger.warning(
            "player_game_grain excluded %d bet rows with null player_id/game_id/label/pcd/pv",
            excluded_bets,
        )
    work = work.loc[valid].copy()
    if work.empty:
        audit = PlayerGameMaterializeAudit(
            input_bet_rows=input_rows,
            output_player_games=0,
            excluded_bet_rows=excluded_bets,
            excluded_player_games=0,
            dq_pcd_span_violations=0,
            dq_pv_span_violations=0,
            dq_table_mismatch=0,
            dq_session_mismatch=0,
        )
        return pd.DataFrame(), audit

    dq = summarize_player_game_dq(
        work,
        pv_span_max_seconds=pv_span_max_seconds,
        pcd_span_max_seconds=pcd_span_max_seconds,
    )
    if exclude_dq_violations and not dq.empty:
        bad_keys = dq.loc[dq["dq_exclude"], list(GROUP_KEY_COLUMNS)]
        if not bad_keys.empty:
            work = work.merge(bad_keys.assign(_drop=1), on=list(GROUP_KEY_COLUMNS), how="left")
            work = work.loc[work["_drop"].isna()].drop(columns=["_drop"])

    if work.empty:
        audit = PlayerGameMaterializeAudit(
            input_bet_rows=input_rows,
            output_player_games=0,
            excluded_bet_rows=excluded_bets,
            excluded_player_games=int(dq["dq_exclude"].sum()) if not dq.empty else 0,
            dq_pcd_span_violations=int((dq["pcd_span_seconds"] > pcd_span_max_seconds).sum()),
            dq_pv_span_violations=int((dq["pv_span_seconds"] > pv_span_max_seconds).sum()),
            dq_table_mismatch=int(dq["table_id_mismatch"].sum()) if "table_id_mismatch" in dq else 0,
            dq_session_mismatch=int(dq["session_id_mismatch"].sum())
            if "session_id_mismatch" in dq
            else 0,
        )
        return pd.DataFrame(), audit

    out = _aggregate_player_game_features(work)
    audit = PlayerGameMaterializeAudit(
        input_bet_rows=input_rows,
        output_player_games=int(len(out)),
        excluded_bet_rows=excluded_bets,
        excluded_player_games=int(dq["dq_exclude"].sum()) if not dq.empty else 0,
        dq_pcd_span_violations=int((dq["pcd_span_seconds"] > pcd_span_max_seconds).sum()),
        dq_pv_span_violations=int((dq["pv_span_seconds"] > pv_span_max_seconds).sum()),
        dq_table_mismatch=int(dq["table_id_mismatch"].sum()) if "table_id_mismatch" in dq else 0,
        dq_session_mismatch=int(dq["session_id_mismatch"].sum()) if "session_id_mismatch" in dq else 0,
    )
    return out, audit


def _sql_quote_path(path: Path) -> str:
    """Escape a filesystem path for DuckDB single-quoted string literals."""

    return str(Path(path).resolve()).replace("\\", "/").replace("'", "''")


def _split_schema_names(parquet_path: Path) -> frozenset[str]:
    """Return column names for one split parquet."""

    return frozenset(pq.ParquetFile(Path(parquet_path).resolve()).schema_arrow.names)


def prepare_bet_splits_for_player_game_materialize(
    splits_dir: Path,
    out_dir: Path,
    *,
    cleaned_bet_root: Path | None = None,
    duckdb_runtime: DuckDbRuntimeConfig,
) -> Path:
    """Write slim bet splits with visibility columns joined from cleaned L0 bets."""

    sd = Path(splits_dir).resolve()
    od = Path(out_dir).resolve()
    od.mkdir(parents=True, exist_ok=True)
    cleaned = Path(cleaned_bet_root or default_cleaned_bet_parquet_path()).resolve()
    cleaned_read = resolved_cleaned_bet_read_parquet_sql(cleaned)
    con = duckdb.connect(database=":memory:")
    try:
        apply_duckdb_runtime_pragmas(con, duckdb_runtime)
        for split in SPLIT_NAMES:
            src = sd / f"{split}.parquet"
            dst = od / f"{split}.parquet"
            if not src.is_file():
                raise FileNotFoundError(f"prepare player_game materialize input requires {src}")
            schema = _split_schema_names(src)
            select_cols = [
                c for c in MATERIALIZE_BET_READ_COLUMNS if c in schema and c not in {PV_COLUMN, SYN_COLUMN}
            ]
            if not select_cols:
                raise ValueError(
                    f"prepare player_game materialize input found no usable split columns in {src!r}; "
                    f"expected subset of {sorted(MATERIALIZE_BET_READ_COLUMNS)!r}",
                )
            split_sql = _sql_quote_path(src)
            out_sql = _sql_quote_path(dst)
            split_select = ", ".join(f's."{c}"' for c in select_cols)
            if PV_COLUMN in schema:
                if SYN_COLUMN in schema:
                    con.execute(
                        f"COPY (SELECT {split_select}, s.\"{PV_COLUMN}\", s.\"{SYN_COLUMN}\" "
                        f"FROM read_parquet('{split_sql}') AS s) "
                        f"TO '{out_sql}' (FORMAT PARQUET)",
                    )
                else:
                    con.execute(
                        f"COPY (SELECT {split_select}, s.\"{PV_COLUMN}\" "
                        f"FROM read_parquet('{split_sql}') AS s) "
                        f"TO '{out_sql}' (FORMAT PARQUET)",
                    )
                continue
            con.execute(
                f"""
                COPY (
                  SELECT
                    {split_select},
                    CAST(v."{PV_COLUMN}" AS TIMESTAMPTZ) AS "{PV_COLUMN}",
                    CAST(v."{SYN_COLUMN}" AS TIMESTAMPTZ) AS "{SYN_COLUMN}"
                  FROM read_parquet('{split_sql}') AS s
                  LEFT JOIN (
                    SELECT
                      CAST(bet_id AS BIGINT) AS bet_id,
                      "{PV_COLUMN}",
                      "{SYN_COLUMN}"
                    FROM {cleaned_read}
                  ) AS v
                  ON CAST(s.bet_id AS BIGINT) = v.bet_id
                ) TO '{out_sql}' (FORMAT PARQUET)
                """,
            )
            null_pv = int(
                con.execute(
                    f"SELECT count(*) FROM read_parquet('{out_sql}') "
                    f"WHERE \"{PV_COLUMN}\" IS NULL",
                ).fetchone()[0],
            )
            if null_pv > 0:
                logger.warning(
                    "player_game materialize input %s has %d rows with null %s after cleaned join",
                    split,
                    null_pv,
                    PV_COLUMN,
                )
    finally:
        con.close()
    return od


def player_game_composition_features(
    *,
    include_settlement: bool = False,
    frame_columns: frozenset[str] | None = None,
) -> tuple[str, ...]:
    """Return ordered player-game composition feature columns for training."""

    cols = list(PLAYER_GAME_COMPOSITION_FEATURES)
    if include_settlement:
        settlement = [
            c for c in PLAYER_GAME_SETTLEMENT_FEATURES if frame_columns is None or c in frame_columns
        ]
        cols.extend(settlement)
    return tuple(cols)


def _read_bet_split_for_materialize(parquet_path: Path) -> pd.DataFrame:
    """Load one bet split with required columns and optional audit fields."""

    p = Path(parquet_path).resolve()
    schema_names = frozenset(pq.ParquetFile(p).schema_arrow.names)
    missing_required = sorted(REQUIRED_BET_COLUMNS - schema_names)
    if missing_required:
        raise ValueError(
            f"player_game materialize schema gate failed: missing {missing_required}; "
            f"path={p!r}, got {sorted(schema_names)!r}",
        )
    cols = [c for c in MATERIALIZE_BET_READ_COLUMNS if c in schema_names]
    return pd.read_parquet(p, columns=cols)


def _apply_training_compat_columns(out: pd.DataFrame) -> pd.DataFrame:
    """Map player-game columns to Step-5-compatible aliases for window/threshold code."""

    frame = out.copy()
    frame[LABEL_COLUMN] = pd.to_numeric(frame[PLAYER_GAME_LABEL_COLUMN], errors="coerce")
    frame[PCD_COLUMN] = pd.to_datetime(frame[PLAYER_GAME_READY_TS_COLUMN], errors="coerce")
    frame[BET_ID_COLUMN] = frame["representative_bet_id"]
    return frame


def materialize_player_game_split_parquet(
    bet_parquet_path: Path,
    out_parquet_path: Path,
    *,
    duckdb_runtime: DuckDbRuntimeConfig | None = None,
    pv_span_max_seconds: int = SCORER_POLL_INTERVAL_SECONDS,
    pcd_span_max_seconds: int = PCD_SPAN_MAX_SECONDS,
) -> PlayerGameMaterializeAudit:
    """Materialize one bet split parquet to player-game grain."""

    if duckdb_runtime is not None:
        return _materialize_player_game_split_parquet_duckdb(
            bet_parquet_path,
            out_parquet_path,
            duckdb_runtime=duckdb_runtime,
            pv_span_max_seconds=pv_span_max_seconds,
            pcd_span_max_seconds=pcd_span_max_seconds,
        )

    bets = _read_bet_split_for_materialize(bet_parquet_path)
    out, audit = aggregate_bets_to_player_game_rows(
        bets,
        pv_span_max_seconds=pv_span_max_seconds,
        pcd_span_max_seconds=pcd_span_max_seconds,
    )
    if out.empty:
        raise ValueError(
            f"player_game materialize produced zero rows from {bet_parquet_path!r}; "
            f"input_bet_rows={audit.input_bet_rows}, excluded_player_games={audit.excluded_player_games}",
        )
    out = _apply_training_compat_columns(out)
    out_path = Path(out_parquet_path).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out.to_parquet(out_path, index=False)
    return audit


def _materialize_player_game_split_parquet_duckdb(
    bet_parquet_path: Path,
    out_parquet_path: Path,
    *,
    duckdb_runtime: DuckDbRuntimeConfig,
    pv_span_max_seconds: int,
    pcd_span_max_seconds: int,
) -> PlayerGameMaterializeAudit:
    """Materialize one bet split to player-game grain using DuckDB (scale-safe)."""

    src = Path(bet_parquet_path).resolve()
    dst = Path(out_parquet_path).resolve()
    dst.parent.mkdir(parents=True, exist_ok=True)
    src_sql = _sql_quote_path(src)
    dst_sql = _sql_quote_path(dst)
    schema = _split_schema_names(src)
    has_casino_win = "casino_win" in schema
    casino_sum_sql = "sum(casino_win) AS pg__casino_win_sum," if has_casino_win else ""
    con = duckdb.connect(database=":memory:")
    try:
        apply_duckdb_runtime_pragmas(con, duckdb_runtime)
        stats = con.execute(
            f"""
            WITH raw AS (
              SELECT * FROM read_parquet('{src_sql}')
            ),
            bets AS (
              SELECT *
              FROM raw
              WHERE player_id IS NOT NULL
                AND game_id IS NOT NULL
                AND walkaway_label IS NOT NULL
                AND payout_complete_dtm IS NOT NULL
                AND prediction_visible_ts_cf IS NOT NULL
            ),
            dq AS (
              SELECT
                date_diff(
                  'second',
                  min(CAST(payout_complete_dtm AS TIMESTAMPTZ)),
                  max(CAST(payout_complete_dtm AS TIMESTAMPTZ))
                ) AS pcd_span_seconds,
                date_diff(
                  'second',
                  min(CAST(prediction_visible_ts_cf AS TIMESTAMPTZ)),
                  max(CAST(prediction_visible_ts_cf AS TIMESTAMPTZ))
                ) AS pv_span_seconds,
                count(DISTINCT table_id) > 1 AS table_id_mismatch,
                count(DISTINCT session_id) > 1 AS session_id_mismatch
              FROM bets
              GROUP BY CAST(player_id AS BIGINT), CAST(game_id AS BIGINT)
            )
            SELECT
              (SELECT count(*) FROM raw) AS input_bet_rows,
              (SELECT count(*) FROM raw) - (SELECT count(*) FROM bets) AS excluded_bet_rows,
              coalesce(sum(
                CASE
                  WHEN pcd_span_seconds > {int(pcd_span_max_seconds)}
                    OR pv_span_seconds > {int(pv_span_max_seconds)}
                    OR table_id_mismatch
                    OR session_id_mismatch
                  THEN 1 ELSE 0
                END
              ), 0) AS excluded_player_games,
              coalesce(sum(CASE WHEN pcd_span_seconds > {int(pcd_span_max_seconds)} THEN 1 ELSE 0 END), 0)
                AS dq_pcd_span_violations,
              coalesce(sum(CASE WHEN pv_span_seconds > {int(pv_span_max_seconds)} THEN 1 ELSE 0 END), 0)
                AS dq_pv_span_violations,
              coalesce(sum(CASE WHEN table_id_mismatch THEN 1 ELSE 0 END), 0) AS dq_table_mismatch,
              coalesce(sum(CASE WHEN session_id_mismatch THEN 1 ELSE 0 END), 0) AS dq_session_mismatch
            FROM dq
            """,
        ).fetchone()
        con.execute(
            f"""
            COPY (
              WITH bets AS (
                SELECT
                  CAST(player_id AS BIGINT) AS player_id,
                  CAST(game_id AS BIGINT) AS game_id,
                  CAST(bet_id AS BIGINT) AS bet_id,
                  CAST(walkaway_label AS INTEGER) AS walkaway_label,
                  CAST(payout_complete_dtm AS TIMESTAMPTZ) AS payout_complete_dtm,
                  CAST(prediction_visible_ts_cf AS TIMESTAMPTZ) AS prediction_visible_ts_cf,
                  CAST(type_of_bet AS VARCHAR) AS type_of_bet,
                  coalesce(CAST(wager AS DOUBLE), 0.0) AS wager,
                  coalesce(CAST(casino_win AS DOUBLE), 0.0) AS casino_win,
                  table_id,
                  session_id
                FROM read_parquet('{src_sql}')
                WHERE player_id IS NOT NULL
                  AND game_id IS NOT NULL
                  AND walkaway_label IS NOT NULL
                  AND payout_complete_dtm IS NOT NULL
                  AND prediction_visible_ts_cf IS NOT NULL
              ),
              dq AS (
                SELECT
                  player_id,
                  game_id,
                  date_diff('second', min(payout_complete_dtm), max(payout_complete_dtm)) AS pcd_span_seconds,
                  date_diff(
                    'second',
                    min(prediction_visible_ts_cf),
                    max(prediction_visible_ts_cf)
                  ) AS pv_span_seconds,
                  count(DISTINCT table_id) > 1 AS table_id_mismatch,
                  count(DISTINCT session_id) > 1 AS session_id_mismatch
                FROM bets
                GROUP BY 1, 2
              ),
              good_groups AS (
                SELECT player_id, game_id
                FROM dq
                WHERE pcd_span_seconds <= {int(pcd_span_max_seconds)}
                  AND pv_span_seconds <= {int(pv_span_max_seconds)}
                  AND NOT table_id_mismatch
                  AND NOT session_id_mismatch
              ),
              filtered AS (
                SELECT b.*
                FROM bets AS b
                INNER JOIN good_groups AS g USING (player_id, game_id)
              ),
              agg AS (
                SELECT
                  player_id,
                  game_id,
                  count(*)::BIGINT AS player_game_bet_count,
                  sum(CASE WHEN upper(type_of_bet) = 'MAIN_BET' THEN 1 ELSE 0 END)::BIGINT
                    AS pg__main_bet_count,
                  sum(CASE WHEN upper(type_of_bet) = 'SIDE_BET' THEN 1 ELSE 0 END)::BIGINT
                    AS pg__side_bet_count,
                  sum(wager) AS pg__wager_sum,
                  max(wager) AS pg__wager_max,
                  sum(CASE WHEN upper(type_of_bet) = 'MAIN_BET' THEN wager ELSE 0.0 END)
                    AS pg__main_wager_sum,
                  sum(CASE WHEN upper(type_of_bet) = 'SIDE_BET' THEN wager ELSE 0.0 END)
                    AS pg__side_wager_sum,
                  {casino_sum_sql}
                  max(walkaway_label)::INTEGER AS player_game_label,
                  min(payout_complete_dtm) AS player_game_payout_complete_dtm,
                  max(prediction_visible_ts_cf) AS player_game_ready_ts,
                  last(
                    bet_id
                    ORDER BY prediction_visible_ts_cf, payout_complete_dtm, bet_id
                  ) AS representative_bet_id
                FROM filtered
                GROUP BY player_id, game_id
              )
              SELECT
                agg.player_id,
                agg.game_id,
                agg.player_game_bet_count,
                agg.pg__main_bet_count,
                agg.pg__side_bet_count,
                agg.pg__wager_sum,
                agg.pg__wager_max,
                agg.pg__main_wager_sum,
                agg.pg__side_wager_sum,
                {"agg.pg__casino_win_sum," if has_casino_win else ""}
                agg.player_game_label,
                agg.player_game_payout_complete_dtm,
                agg.player_game_ready_ts,
                dq.pcd_span_seconds AS pg__pcd_span_seconds,
                dq.pv_span_seconds AS pg__prediction_visible_span_seconds,
                agg.representative_bet_id,
                (agg.pg__main_bet_count > 0) AS pg__has_main_bet,
                (agg.pg__side_bet_count > 0) AS pg__has_side_bet,
                (
                  agg.pg__side_bet_count > 0
                  AND agg.pg__main_bet_count = 0
                ) AS pg__side_only_flag,
                CASE
                  WHEN agg.pg__main_wager_sum = 0 THEN 0.0
                  ELSE agg.pg__side_wager_sum / agg.pg__main_wager_sum
                END AS pg__side_to_main_wager_ratio,
                agg.player_game_label AS walkaway_label,
                agg.player_game_ready_ts AS payout_complete_dtm,
                agg.representative_bet_id AS bet_id
              FROM agg
              INNER JOIN dq USING (player_id, game_id)
            ) TO '{dst_sql}' (FORMAT PARQUET)
            """,
        )
    finally:
        con.close()

    if stats is None:
        stats = (0, 0, 0, 0, 0, 0, 0)
    out_count = int(pq.ParquetFile(dst).metadata.num_rows)
    audit = PlayerGameMaterializeAudit(
        input_bet_rows=int(stats[0]),
        output_player_games=out_count,
        excluded_bet_rows=int(stats[1]),
        excluded_player_games=int(stats[2]),
        dq_pcd_span_violations=int(stats[3]),
        dq_pv_span_violations=int(stats[4]),
        dq_table_mismatch=int(stats[5]),
        dq_session_mismatch=int(stats[6]),
    )
    if audit.output_player_games <= 0:
        raise ValueError(
            f"player_game duckdb materialize produced zero rows from {src!r}; audit={audit}",
        )
    return audit


def materialize_player_game_splits(
    splits_dir: Path,
    out_dir: Path,
    *,
    duckdb_runtime: DuckDbRuntimeConfig | None = None,
) -> PlayerGameSplitMaterializeResult:
    """Materialize train/val/test bet splits under ``out_dir``."""

    sd = Path(splits_dir).resolve()
    od = Path(out_dir).resolve()
    od.mkdir(parents=True, exist_ok=True)
    audits: dict[str, PlayerGameMaterializeAudit] = {}
    for split in SPLIT_NAMES:
        src = sd / f"{split}.parquet"
        if not src.is_file():
            raise FileNotFoundError(f"player_game materialize requires split parquet at {src}")
        audits[split] = materialize_player_game_split_parquet(
            src,
            od / f"{split}.parquet",
            duckdb_runtime=duckdb_runtime,
        )
    return PlayerGameSplitMaterializeResult(out_dir=od, audits=audits)


def _load_player_game_split_frame(
    parquet_path: Path,
    *,
    feature_columns: tuple[str, ...],
) -> pd.DataFrame:
    """Load one player-game split with model features and compat columns."""

    cols = list(feature_columns) + [
        LABEL_COLUMN,
        PCD_COLUMN,
        PLAYER_ID_COLUMN,
        GAME_ID_COLUMN,
        BET_ID_COLUMN,
        PLAYER_GAME_LABEL_COLUMN,
        PLAYER_GAME_READY_TS_COLUMN,
    ]
    p = Path(parquet_path).resolve()
    schema_names = frozenset(pq.ParquetFile(p).schema_arrow.names)
    missing = sorted(frozenset(cols).difference(schema_names))
    if missing:
        raise ValueError(
            f"player_game split schema gate failed: missing {missing}; "
            f"path={p!r}, got {sorted(schema_names)!r}",
        )
    return pd.read_parquet(p, columns=cols)


def _build_player_game_candidates(df: pd.DataFrame, scores: np.ndarray) -> pd.DataFrame:
    """Build operational alert candidate frame from native player-game rows."""

    if len(df) != int(len(scores)):
        raise ValueError(
            f"_build_player_game_candidates: df length {len(df)} != scores length {len(scores)}",
        )
    out = df[
        [
            PLAYER_ID_COLUMN,
            GAME_ID_COLUMN,
            BET_ID_COLUMN,
            PLAYER_GAME_LABEL_COLUMN,
            PLAYER_GAME_READY_TS_COLUMN,
        ]
    ].copy()
    out[SCORE_COLUMN] = np.asarray(scores, dtype=np.float64).reshape(-1)
    out[PG_LABEL_COLUMN] = pd.to_numeric(out[PLAYER_GAME_LABEL_COLUMN], errors="coerce")
    out[ALERT_TS_COLUMN] = pd.to_datetime(out[PLAYER_GAME_READY_TS_COLUMN], errors="coerce")
    out[TIE_BREAK_COLUMN] = pd.to_numeric(out[BET_ID_COLUMN], errors="coerce")
    return out[
        [
            PLAYER_ID_COLUMN,
            GAME_ID_COLUMN,
            SCORE_COLUMN,
            PG_LABEL_COLUMN,
            ALERT_TS_COLUMN,
            TIE_BREAK_COLUMN,
        ]
    ]


def enrich_player_game_splits_with_baseline_bet_features(
    pg_txn_splits_dir: Path,
    bet_splits_dir: Path,
    out_dir: Path,
    *,
    feature_columns: tuple[str, ...],
    duckdb_runtime: DuckDbRuntimeConfig,
) -> dict[str, Any]:
    """Join MVP baseline bet features from the representative bet onto PG splits.

    Non-``txn__*`` columns come from the bet row matching ``representative_bet_id``.
    ``txn__*`` columns are retained from ``pg_txn_splits_dir`` (PIT at ``player_game_ready_ts``).
    """

    if not feature_columns:
        raise ValueError("enrich_player_game_splits_with_baseline_bet_features requires feature_columns")
    pg_dir = Path(pg_txn_splits_dir).resolve()
    bet_dir = Path(bet_splits_dir).resolve()
    od = Path(out_dir).resolve()
    od.mkdir(parents=True, exist_ok=True)
    bet_cols = tuple(c for c in feature_columns if not c.startswith("txn__"))
    txn_cols = tuple(c for c in feature_columns if c.startswith("txn__"))
    bet_schema = _split_schema_names(bet_dir / "train.parquet")
    pg_schema = _split_schema_names(pg_dir / "train.parquet")
    missing_bet = sorted(frozenset(bet_cols).difference(bet_schema))
    missing_txn = sorted(frozenset(txn_cols).difference(pg_schema))
    if missing_bet:
        raise ValueError(
            f"baseline bet enrich missing columns in bet splits: {missing_bet}; "
            f"bet_dir={bet_dir!r}",
        )
    if missing_txn:
        raise ValueError(
            f"baseline bet enrich missing txn columns in pg_txn splits: {missing_txn}; "
            f"pg_dir={pg_dir!r}",
        )
    bet_select = ",\n        ".join(f"bet.{col} AS {col}" for col in bet_cols)
    split_stats: dict[str, dict[str, int]] = {}
    con = duckdb.connect(database=":memory:")
    try:
        apply_duckdb_runtime_pragmas(con, duckdb_runtime)
        for split in ("train", "val", "test"):
            pg_src = pg_dir / f"{split}.parquet"
            bet_src = bet_dir / f"{split}.parquet"
            dst = od / f"{split}.parquet"
            if not pg_src.is_file() or not bet_src.is_file():
                raise FileNotFoundError(
                    f"baseline bet enrich requires {pg_src} and {bet_src}",
                )
            pg_sql = _sql_quote_path(pg_src)
            bet_sql = _sql_quote_path(bet_src)
            dst_sql = _sql_quote_path(dst)
            con.execute(
                f"""
                COPY (
                  SELECT
                    pg.*,
                    {bet_select}
                  FROM read_parquet('{pg_sql}') AS pg
                  INNER JOIN read_parquet('{bet_sql}') AS bet
                    ON pg.representative_bet_id = bet.bet_id
                ) TO '{dst_sql}' (FORMAT PARQUET, COMPRESSION SNAPPY)
                """,
            )
            pg_count = int(
                con.execute(f"SELECT count(*) FROM read_parquet('{pg_sql}')").fetchone()[0],
            )
            out_count = int(
                con.execute(f"SELECT count(*) FROM read_parquet('{dst_sql}')").fetchone()[0],
            )
            split_stats[split] = {
                "input_player_games": pg_count,
                "output_player_games": out_count,
                "dropped_unmatched": pg_count - out_count,
            }
            if out_count < pg_count:
                logger.warning(
                    "[PG-B1] %s baseline enrich dropped %d/%d unmatched representative bets",
                    split,
                    pg_count - out_count,
                    pg_count,
                )
    finally:
        con.close()
    meta: dict[str, Any] = {
        "join_grain": "representative_bet_id = bet_id",
        "bet_feature_columns": list(bet_cols),
        "txn_feature_columns": list(txn_cols),
        "bet_splits_dir": str(bet_dir),
        "pg_txn_splits_dir": str(pg_dir),
        "split_stats": split_stats,
    }
    logger.info("[PG-B1] enriched baseline parity splits %s → %s stats=%s", pg_dir, od, split_stats)
    return meta


def _resolve_pg_objective(
    objective: HighTierObjectiveConfig | None,
    objective_min_precision: float,
) -> HighTierObjectiveConfig:
    """Merge explicit objective config with legacy min-precision override."""

    if objective is not None:
        return objective
    return HighTierObjectiveConfig(min_precision=float(objective_min_precision))


def train_player_game_lgbm_from_splits(
    *,
    pg_splits_dir: Path,
    duckdb_runtime: DuckDbRuntimeConfig,
    objective_min_precision: float,
    random_seed: int,
    output_dir: Path,
    feature_columns: tuple[str, ...],
    step5: Step5TrainConfig | None = None,
    objective: HighTierObjectiveConfig | None = None,
    persist_training_metrics: bool = True,
) -> PlayerGameTrainResult:
    """Train native player-game LightGBM on materialized player-game splits."""

    if not feature_columns:
        raise ValueError("train_player_game_lgbm_from_splits requires non-empty feature_columns")
    feat_cols = tuple(feature_columns)
    cfg = step5 or Step5TrainConfig(skip_optuna=True)
    objective_cfg = _resolve_pg_objective(objective, float(objective_min_precision))
    sd = Path(pg_splits_dir).resolve()
    train_p = sd / "train.parquet"
    val_p = sd / "val.parquet"
    test_p = sd / "test.parquet"
    for pth in (train_p, val_p, test_p):
        if not pth.is_file():
            raise FileNotFoundError(f"player_game train requires split parquet at {pth}")

    wh_train = _b5.split_window_hours_from_parquet(train_p, duckdb_runtime=duckdb_runtime)
    wh_val = _b5.split_window_hours_from_parquet(val_p, duckdb_runtime=duckdb_runtime)
    wh_test = _b5.split_window_hours_from_parquet(test_p, duckdb_runtime=duckdb_runtime)

    df_tr = _load_player_game_split_frame(train_p, feature_columns=feat_cols)
    df_va = _load_player_game_split_frame(val_p, feature_columns=feat_cols)
    df_te = _load_player_game_split_frame(test_p, feature_columns=feat_cols)
    X_tr, y_tr = _b5._prepare_xy(df_tr, feature_columns=feat_cols)
    X_va, y_va = _b5._prepare_xy(df_va, feature_columns=feat_cols)
    X_te, y_te = _b5._prepare_xy(df_te, feature_columns=feat_cols)

    cat_cols = [c for c in feat_cols if c in _b5.CAT_COLUMNS]
    union_cats: dict[str, pd.Index] = {}
    for col in cat_cols:
        combined = pd.concat(
            [X_tr[col].astype(str), X_va[col].astype(str), X_te[col].astype(str)],
            axis=0,
            ignore_index=True,
        )
        union_cats[col] = pd.Index(pd.unique(combined))
    for col in cat_cols:
        X_tr[col] = pd.Categorical(X_tr[col], categories=union_cats[col])
        X_va[col] = pd.Categorical(X_va[col], categories=union_cats[col])
        X_te[col] = pd.Categorical(X_te[col], categories=union_cats[col])

    val_pos = int(np.sum(y_va == 1))
    if val_pos < 1 or int(np.sum(y_va == 0)) < 1:
        raise ValueError(
            f"player_game validation must have pos and neg labels; "
            f"positives={val_pos}, n={len(y_va)}",
        )

    t0 = time.perf_counter()
    hp = _b5._baseline_lgb_params(cfg, int(random_seed))
    model = _b5._train_one_lgbm(
        X_tr,
        y_tr,
        X_va,
        y_va,
        hp,
        early_stopping_rounds=int(cfg.early_stopping_rounds),
    )
    val_scores = model.predict_proba(X_va)[:, 1]
    val_pick = _b5.pick_threshold_precision_floor(
        y_va,
        val_scores,
        min_precision=float(objective_cfg.min_precision),
    )
    thr = float(val_pick.threshold)
    train_scores = model.predict_proba(X_tr)[:, 1]
    test_scores = model.predict_proba(X_te)[:, 1]

    block_tr = split_metrics_block("train", y_tr, train_scores, thr, window_hours=wh_train)
    block_va = split_metrics_block("val", y_va, val_scores, thr, window_hours=wh_val)
    block_te = split_metrics_block("test", y_te, test_scores, thr, window_hours=wh_test)
    cand_va = _build_player_game_candidates(df_va, val_scores)
    cand_te = _build_player_game_candidates(df_te, test_scores)
    block_va_op = operational_simulated_metrics_block(
        "val",
        cand_va,
        thr,
        cooldown_min=int(cfg.player_alert_policy.cooldown_min),
        window_hours=wh_val,
    )
    block_te_op = operational_simulated_metrics_block(
        "test",
        cand_te,
        thr,
        cooldown_min=int(cfg.player_alert_policy.cooldown_min),
        window_hours=wh_test,
    )
    elapsed = round(time.perf_counter() - t0, 3)
    policy_meta = build_player_alert_policy_metadata(
        cfg.player_alert_policy,
        train_alert_ts_source=PLAYER_GAME_READY_TS_COLUMN,
    )
    report: dict[str, Any] = {
        "model_grain": MODEL_GRAIN_PLAYER_GAME,
        "evaluation_grain": MODEL_GRAIN_PLAYER_GAME,
        "ready_time_column": PLAYER_GAME_READY_TS_COLUMN,
        "score_aggregation": SCORE_AGGREGATION_NATIVE,
        "label_aggregation": "max",
        "player_game_train_seconds": elapsed,
        "player_game_feature_columns": list(feat_cols),
        "player_game_threshold": thr,
        "player_game_val_pick_feasible": val_pick.feasible,
        "player_game_val_precision_at_pick": val_pick.precision,
        "player_game_val_recall_at_pick": val_pick.recall,
        "player_game_min_precision": float(objective_cfg.min_precision),
        "player_game_optuna_skipped": bool(cfg.skip_optuna),
        **block_tr,
        **block_va,
        **block_te,
        **block_va_op,
        **block_te_op,
        **policy_meta,
    }
    out_dir = Path(output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    model_path = (out_dir / DEFAULT_MODEL_FILENAME).resolve()
    with open(model_path, "wb") as f:
        pickle.dump(
            {
                "model": model,
                "feature_columns": list(feat_cols),
                "categorical_columns": list(cat_cols),
                "category_categories": {c: union_cats[c].tolist() for c in cat_cols},
                "threshold": thr,
                "model_grain": MODEL_GRAIN_PLAYER_GAME,
                "score_aggregation": SCORE_AGGREGATION_NATIVE,
                "ready_time_column": PLAYER_GAME_READY_TS_COLUMN,
                "min_precision": float(objective_cfg.min_precision),
                "val_pick_feasible": val_pick.feasible,
                **policy_meta,
            },
            f,
            protocol=pickle.HIGHEST_PROTOCOL,
        )
    metrics_path = (out_dir / DEFAULT_METRICS_FILENAME).resolve()
    report["model_path"] = str(model_path)
    report["training_metrics_path"] = str(metrics_path)
    if persist_training_metrics:
        metrics_path.write_text(
            json.dumps(slim_training_metrics_body(report), indent=2, default=str),
            encoding="utf-8",
        )
    return PlayerGameTrainResult(
        model_path=model_path,
        metrics_path=metrics_path,
        report=report,
        threshold=thr,
    )
