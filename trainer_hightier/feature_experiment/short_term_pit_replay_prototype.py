"""Event replay prototype for bounded short-term PIT materialization.

Prototype only: not wired into trainer Step 3.5. Output must match the existing
bounded DuckDB materializer for supported columns before any integration.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import duckdb
import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from trainer_hightier.config import (
    DuckDbRuntimeConfig,
    HightierServingConfig,
    default_hightier_serving_config,
)
from trainer_hightier.utils.canonical_mapping import default_canonical_mapping_parquet_path
from trainer_hightier.utils.duckdb_runtime import apply_duckdb_runtime_pragmas

logger = logging.getLogger(__name__)

PROTOTYPE_OUTPUT_COLUMNS: Final[tuple[str, ...]] = (
    "bet_id",
    "fe__bets_cnt__w15m",
    "fe__wager_sum__w15m",
    "fe__time_since_last_bet_sec",
    "fe__odds__payout_odds_step_ratio",
    "bet__bets_cnt__w1h",
)

_US: Final[pd.Timedelta] = pd.Timedelta(microseconds=1)
_W15: Final[pd.Timedelta] = pd.Timedelta(minutes=15)
_W1H: Final[pd.Timedelta] = pd.Timedelta(hours=1)


@dataclass(frozen=True)
class _PoolEvent:
    """Single cleaned bet row in replay order."""

    bet_id: float
    player_id: int
    canonical_id: str
    pcd: pd.Timestamp
    wager: float
    payout_odds: float


@dataclass
class _EntityReplayState:
    """Rolling prior events for one ``(canonical_id, player_id)`` key."""

    events: deque[_PoolEvent] = field(default_factory=deque)
    max_queue_len: int = 0

    def append(self, event: _PoolEvent) -> None:
        """Append one processed event to entity history."""
        self.events.append(event)
        self.max_queue_len = max(self.max_queue_len, len(self.events))


def _path_esc(path: Path) -> str:
    return str(Path(path).resolve()).replace("\\", "/")


def unique_int_player_ids(values: pd.Series | np.ndarray | list[object]) -> tuple[int, ...]:
    """Return sorted unique player ids from mixed numeric input."""
    if isinstance(values, pd.Series):
        nums = pd.to_numeric(values, errors="coerce")
    else:
        nums = pd.to_numeric(pd.Series(list(values)), errors="coerce")
    return tuple(sorted({int(pid) for pid in nums.dropna().astype(int).tolist()}))


def _entity_key(canonical_id: str, player_id: int) -> tuple[str, int]:
    return str(canonical_id).strip(), int(player_id)


def _gaming_day_event_select_sql(
    training_parquet: Path,
    *,
    hk_tz: str,
) -> str:
    """Return a DuckDB expression for ``gaming_day_event`` on training rows."""
    names = set(pq.read_schema(Path(training_parquet).resolve()).names)
    if "gaming_day_event" in names:
        return "CAST(gaming_day_event AS TIMESTAMP) AS gaming_day_event"
    return (
        f"CAST((CAST(payout_complete_dtm AS TIMESTAMPTZ) AT TIME ZONE '{hk_tz}')::DATE "
        "AS TIMESTAMP) AS gaming_day_event"
    )


def _load_target_bets(
    training_parquet: Path,
    *,
    payout_yyyymm: str,
    target_limit: int | None,
    duckdb_runtime: DuckDbRuntimeConfig,
    hk_tz: str = "Asia/Hong_Kong",
) -> pd.DataFrame:
    """Load ordered target training bets for one payout month."""
    ym = str(payout_yyyymm).strip()
    if len(ym) != 6 or not ym.isdigit():
        raise ValueError(f"payout_yyyymm must be six digits, got {payout_yyyymm!r}")
    limit_sql = ""
    if target_limit is not None:
        if int(target_limit) < 1:
            raise ValueError(f"target_limit must be >= 1, got {target_limit}")
        limit_sql = f" LIMIT {int(target_limit)}"
    t_esc = _path_esc(training_parquet)
    gde_sql = _gaming_day_event_select_sql(training_parquet, hk_tz=hk_tz)
    con = duckdb.connect(database=":memory:")
    try:
        apply_duckdb_runtime_pragmas(con, duckdb_runtime)
        return con.execute(
            f"""
            SELECT
              TRY_CAST(bet_id AS DOUBLE) AS bet_id,
              TRY_CAST(player_id AS BIGINT) AS player_id,
              CAST(payout_complete_dtm AS TIMESTAMPTZ) AS payout_complete_dtm,
              TRY_CAST(wager AS DOUBLE) AS wager,
              TRY_CAST(payout_odds AS DOUBLE) AS payout_odds,
              {gde_sql}
            FROM read_parquet('{t_esc}')
            WHERE TRY_CAST(bet_id AS DOUBLE) IS NOT NULL
              AND payout_complete_dtm IS NOT NULL
              AND TRY_CAST(player_id AS BIGINT) IS NOT NULL
              AND strftime(CAST(payout_complete_dtm AS TIMESTAMPTZ), '%Y%m') = '{ym}'
            ORDER BY CAST(payout_complete_dtm AS TIMESTAMPTZ) ASC,
                     TRY_CAST(bet_id AS DOUBLE) ASC
            {limit_sql}
            """,
        ).fetchdf()
    finally:
        con.close()


def _attach_canonical_id(bets: pd.DataFrame, mapping_parquet: Path) -> pd.DataFrame:
    """Join ``canonical_id`` onto bet rows."""
    from trainer_hightier.serving.feature_builder import attach_canonical_id

    out = bets.copy()
    out["player_id"] = pd.to_numeric(out["player_id"], errors="coerce")
    return attach_canonical_id(out, mapping_parquet=mapping_parquet)


def _canonical_by_player(
    mapping_parquet: Path,
    player_ids: tuple[int, ...],
) -> dict[int, str]:
    """Resolve ``canonical_id`` for replay pool player ids."""
    if not player_ids:
        return {}
    stub = pd.DataFrame({"player_id": sorted(set(int(pid) for pid in player_ids))})
    mapped = _attach_canonical_id(stub, mapping_parquet)
    return {
        int(row.player_id): str(row.canonical_id).strip()
        for row in mapped.itertuples(index=False)
        if pd.notna(row.canonical_id) and str(row.canonical_id).strip()
    }


def _load_replay_events(
    cleaned_root: Path,
    *,
    payout_yyyymm: str,
    player_ids: tuple[int, ...],
    duckdb_runtime: DuckDbRuntimeConfig,
    hk_tz: str,
) -> pd.DataFrame:
    """Load partition-pruned cleaned bet rows for replay."""
    from trainer_hightier.utils.cleaned_bet_pool_read import open_month_hot_pool_session

    unique_ids = unique_int_player_ids(player_ids)
    if not unique_ids:
        return pd.DataFrame(
            columns=[
                "bet_id",
                "player_id",
                "payout_complete_dtm",
                "wager",
                "casino_win",
                "theo_win",
                "is_back_bet",
                "payout_odds",
            ],
        )
    session = open_month_hot_pool_session(
        cleaned_root,
        payout_yyyymm=str(payout_yyyymm),
        duckdb_runtime=duckdb_runtime,
        hk_tz=hk_tz,
        restrict_player_ids=unique_ids,
    )
    try:
        ids_sql = ",".join(str(int(pid)) for pid in unique_ids)
        return session.conn.execute(
            f"""
            SELECT
              TRY_CAST(bet_id AS DOUBLE) AS bet_id,
              TRY_CAST(player_id AS BIGINT) AS player_id,
              CAST(payout_complete_dtm AS TIMESTAMPTZ) AS payout_complete_dtm,
              CAST(gaming_day_event AS TIMESTAMP) AS gaming_day_event,
              TRY_CAST(wager AS DOUBLE) AS wager,
              TRY_CAST(casino_win AS DOUBLE) AS casino_win,
              TRY_CAST(theo_win AS DOUBLE) AS theo_win,
              TRY_CAST(is_back_bet AS INTEGER) AS is_back_bet,
              TRY_CAST(payout_odds AS DOUBLE) AS payout_odds
            FROM {session.table_name}
            WHERE TRY_CAST(player_id AS BIGINT) IN ({ids_sql})
            ORDER BY CAST(payout_complete_dtm AS TIMESTAMPTZ) ASC,
                     TRY_CAST(bet_id AS DOUBLE) ASC
            """,
        ).fetchdf()
    finally:
        session.close()


def _iter_pool_events(
    events_df: pd.DataFrame,
    canonical_by_player: dict[int, str],
) -> Iterator[_PoolEvent]:
    """Yield replay events in chronological order."""
    if events_df.empty:
        return
    work = events_df.copy()
    work["payout_complete_dtm"] = pd.to_datetime(work["payout_complete_dtm"], errors="coerce", utc=True)
    work["bet_id"] = pd.to_numeric(work["bet_id"], errors="coerce")
    work["player_id"] = pd.to_numeric(work["player_id"], errors="coerce")
    work["wager"] = pd.to_numeric(work["wager"], errors="coerce").fillna(0.0)
    work["payout_odds"] = pd.to_numeric(work["payout_odds"], errors="coerce")
    work = work.dropna(subset=["bet_id", "player_id", "payout_complete_dtm"])
    work = work.sort_values(["payout_complete_dtm", "bet_id"], kind="mergesort")
    for row in work.itertuples(index=False):
        pid = int(row.player_id)
        cid = canonical_by_player.get(pid, "")
        if not cid:
            continue
        yield _PoolEvent(
            bet_id=float(row.bet_id),
            player_id=pid,
            canonical_id=cid,
            pcd=pd.Timestamp(row.payout_complete_dtm),
            wager=float(row.wager),
            payout_odds=float(row.payout_odds) if pd.notna(row.payout_odds) else np.nan,
        )


def _range_window_events(
    state: _EntityReplayState,
    *,
    pool_start: pd.Timestamp,
    scoring_pcd: pd.Timestamp,
    window: pd.Timedelta,
) -> list[_PoolEvent]:
    """Return pool rows inside a DuckDB RANGE ``PRECEDING`` window."""
    window_end = scoring_pcd - _US
    window_start = scoring_pcd - window
    return [
        event
        for event in state.events
        if pool_start <= event.pcd <= window_end and event.pcd >= window_start
    ]


def _row_lag_prior_event(
    state: _EntityReplayState,
    *,
    pool_start: pd.Timestamp,
    scoring_pcd: pd.Timestamp,
    target_bet_id: float,
) -> _PoolEvent | None:
    """Return the immediate predecessor row in the bounded pool slice."""
    candidates = [
        event
        for event in state.events
        if pool_start <= event.pcd <= scoring_pcd
        and (event.pcd, event.bet_id) < (scoring_pcd, float(target_bet_id))
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda event: (event.pcd, event.bet_id))


def _emit_prototype_features(
    state: _EntityReplayState,
    *,
    pool_start: pd.Timestamp,
    scoring_pcd: pd.Timestamp,
    target_bet_id: float,
    payout_odds: float,
    trial_pool_start: pd.Timestamp,
) -> dict[str, float]:
    """Compute prototype columns from entity state before current event update."""
    prior_15m = _range_window_events(
        state,
        pool_start=pool_start,
        scoring_pcd=scoring_pcd,
        window=_W15,
    )
    prior_1h = _range_window_events(
        state,
        pool_start=trial_pool_start,
        scoring_pcd=scoring_pcd,
        window=_W1H,
    )
    lag_prior = _row_lag_prior_event(
        state,
        pool_start=pool_start,
        scoring_pcd=scoring_pcd,
        target_bet_id=target_bet_id,
    )
    out: dict[str, float] = {
        "fe__bets_cnt__w15m": float(len(prior_15m)),
        "fe__wager_sum__w15m": float(sum(event.wager for event in prior_15m)),
        "bet__bets_cnt__w1h": float(len(prior_1h)),
        "fe__time_since_last_bet_sec": np.nan,
        "fe__odds__payout_odds_step_ratio": np.nan,
    }
    if lag_prior is not None:
        gap = (scoring_pcd - lag_prior.pcd).total_seconds()
        out["fe__time_since_last_bet_sec"] = float(gap)
        if (
            np.isfinite(lag_prior.payout_odds)
            and lag_prior.payout_odds > 1e-9
            and np.isfinite(payout_odds)
        ):
            out["fe__odds__payout_odds_step_ratio"] = float(payout_odds / lag_prior.payout_odds)
    return out


def _replay_features(
    events_df: pd.DataFrame,
    targets: pd.DataFrame,
    bounds: pd.DataFrame,
    *,
    canonical_by_player: dict[int, str],
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Replay month events and emit prototype features for target bets."""
    bounds_idx = bounds.set_index(pd.to_numeric(bounds["bet_id"], errors="coerce"))
    trial_pool_start = pd.Timestamp(bounds["pool_start"].min())
    target_ids = {
        float(bid)
        for bid in pd.to_numeric(targets["bet_id"], errors="coerce").dropna().tolist()
    }
    states: dict[tuple[str, int], _EntityReplayState] = {}
    rows: list[dict[str, float]] = []
    max_queue = 0
    for event in _iter_pool_events(events_df, canonical_by_player):
        key = _entity_key(event.canonical_id, event.player_id)
        state = states.setdefault(key, _EntityReplayState())
        if event.bet_id in target_ids:
            bound = bounds_idx.loc[event.bet_id]
            pool_start = pd.Timestamp(bound["pool_start"])
            scoring_pcd = pd.Timestamp(bound["scoring_pcd"])
            emitted = _emit_prototype_features(
                state,
                pool_start=pool_start,
                scoring_pcd=scoring_pcd,
                target_bet_id=event.bet_id,
                payout_odds=event.payout_odds,
                trial_pool_start=trial_pool_start,
            )
            rows.append({"bet_id": event.bet_id, **emitted})
        state.append(event)
        max_queue = max(max_queue, state.max_queue_len)
    out = pd.DataFrame(rows)
    if out.empty:
        out = pd.DataFrame(columns=list(PROTOTYPE_OUTPUT_COLUMNS))
    metrics = {
        "input_event_rows": int(len(events_df)),
        "target_rows": int(len(targets)),
        "output_rows": int(len(out)),
        "max_state_keys": int(len(states)),
        "max_queue_len": int(max_queue),
    }
    return out, metrics


def materialize_short_term_replay_prototype(
    *,
    cleaned_bet_parquet: Path,
    training_parquet_for_bet_ids: Path,
    out_parquet: Path,
    payout_yyyymm: str,
    duckdb_runtime: DuckDbRuntimeConfig,
    canonical_mapping_parquet: Path | None = None,
    target_limit: int | None = None,
    serving_cfg: HightierServingConfig | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Materialize prototype short PIT features via one-pass event replay."""
    cfg = serving_cfg or default_hightier_serving_config()
    cmap = (
        Path(canonical_mapping_parquet).resolve()
        if canonical_mapping_parquet is not None
        else default_canonical_mapping_parquet_path().resolve()
    )
    if not cmap.is_file():
        raise FileNotFoundError(f"canonical mapping parquet missing: {cmap}")
    t0 = time.perf_counter()
    targets = _load_target_bets(
        Path(training_parquet_for_bet_ids).resolve(),
        payout_yyyymm=str(payout_yyyymm),
        target_limit=target_limit,
        duckdb_runtime=duckdb_runtime,
        hk_tz=cfg.hk_tz,
    )
    if targets.empty:
        raise ValueError(
            f"replay prototype found no target bets for month={payout_yyyymm!r} "
            f"in {training_parquet_for_bet_ids}",
        )
    targets = _attach_canonical_id(targets, cmap)
    player_ids = tuple(
        int(pid)
        for pid in pd.to_numeric(targets["player_id"], errors="coerce").dropna().astype(int).tolist()
    )
    events_df = _load_replay_events(
        Path(cleaned_bet_parquet).resolve(),
        payout_yyyymm=str(payout_yyyymm),
        player_ids=player_ids,
        duckdb_runtime=duckdb_runtime,
        hk_tz=cfg.hk_tz,
    )
    from trainer_hightier.serving.scorer import compute_scoring_bounds_for_bets

    bounds = compute_scoring_bounds_for_bets(targets, cfg=cfg)
    pool_player_ids = tuple(
        int(pid)
        for pid in pd.to_numeric(events_df["player_id"], errors="coerce").dropna().astype(int).tolist()
    )
    canonical_by_player = _canonical_by_player(cmap, pool_player_ids)
    features, replay_metrics = _replay_features(
        events_df,
        targets,
        bounds,
        canonical_by_player={int(k): str(v) for k, v in canonical_by_player.items()},
    )
    dst = Path(out_parquet).resolve()
    dst.parent.mkdir(parents=True, exist_ok=True)
    features[list(PROTOTYPE_OUTPUT_COLUMNS)].to_parquet(dst, index=False)
    elapsed = round(time.perf_counter() - t0, 6)
    metrics = {
        **replay_metrics,
        "elapsed_seconds": elapsed,
        "rows_per_second": round(
            float(replay_metrics["input_event_rows"]) / elapsed,
            3,
        )
        if elapsed > 0
        else None,
        "payout_yyyymm": str(payout_yyyymm),
        "prototype_columns": list(PROTOTYPE_OUTPUT_COLUMNS),
    }
    logger.info(
        "[short_pit_replay_prototype] wrote %s rows=%d elapsed=%.3fs",
        dst.name,
        len(features),
        elapsed,
    )
    return dst, metrics


def evaluate_replay_go_no_go(
    *,
    parity_passed: bool,
    replay_elapsed_seconds: float,
    bounded_elapsed_seconds: float,
    min_speedup_ratio: float = 3.0,
) -> dict[str, Any]:
    """Summarize prototype go/no-go against parity and speed thresholds."""
    speedup = (
        bounded_elapsed_seconds / replay_elapsed_seconds
        if replay_elapsed_seconds > 0
        else None
    )
    expand = bool(
        parity_passed
        and speedup is not None
        and speedup >= float(min_speedup_ratio),
    )
    return {
        "decision": "expand" if expand else "stop_or_optimize",
        "parity_passed": bool(parity_passed),
        "speedup_ratio": speedup,
        "min_speedup_ratio": float(min_speedup_ratio),
    }


def compare_replay_to_oracle(
    replay_df: pd.DataFrame,
    oracle_df: pd.DataFrame,
    *,
    columns: tuple[str, ...] = PROTOTYPE_OUTPUT_COLUMNS[1:],
    float_tol: float = 1e-9,
) -> dict[str, Any]:
    """Compare replay output against bounded DuckDB oracle columns."""
    left = replay_df.copy()
    right = oracle_df.copy()
    left["bet_id"] = pd.to_numeric(left["bet_id"], errors="coerce")
    right["bet_id"] = pd.to_numeric(right["bet_id"], errors="coerce")
    merged = left.merge(right, on="bet_id", how="inner", suffixes=("_replay", "_oracle"))
    if merged.empty:
        raise ValueError("replay/oracle merge produced zero rows")
    report: dict[str, Any] = {"compared_rows": int(len(merged)), "columns": {}}
    for col in columns:
        replay_col = f"{col}_replay" if f"{col}_replay" in merged.columns else col
        oracle_col = f"{col}_oracle" if f"{col}_oracle" in merged.columns else col
        if replay_col not in merged.columns or oracle_col not in merged.columns:
            report["columns"][col] = {"status": "missing"}
            continue
        r = pd.to_numeric(merged[replay_col], errors="coerce")
        o = pd.to_numeric(merged[oracle_col], errors="coerce")
        both_nan = r.isna() & o.isna()
        close = np.isclose(r, o, rtol=1e-6, atol=float_tol, equal_nan=True)
        mismatch = ~(both_nan | close)
        sample_ids = merged.loc[mismatch, "bet_id"].head(5).tolist()
        report["columns"][col] = {
            "mismatch_count": int(mismatch.sum()),
            "sample_bet_ids": sample_ids,
        }
    report["passed"] = all(
        info.get("mismatch_count", 0) == 0 for info in report["columns"].values()
    )
    return report


def benchmark_replay_vs_bounded(
    *,
    cleaned_bet_parquet: Path,
    training_parquet_for_bet_ids: Path,
    payout_yyyymm: str,
    duckdb_runtime: DuckDbRuntimeConfig,
    canonical_mapping_parquet: Path | None = None,
    target_limit: int = 1000,
    out_dir: Path | None = None,
) -> dict[str, Any]:
    """Run replay and bounded oracle on the same bounded sample."""
    from trainer_hightier.feature_experiment.materialize_fe_derived import (
        materialize_fe_derived_short_term_parquet,
    )

    root = Path(out_dir or Path(training_parquet_for_bet_ids).resolve().parent / "replay_prototype_bench")
    root.mkdir(parents=True, exist_ok=True)
    train_esc = _path_esc(Path(training_parquet_for_bet_ids).resolve())
    ym = str(payout_yyyymm).strip()
    con = duckdb.connect(database=":memory:")
    try:
        apply_duckdb_runtime_pragmas(con, duckdb_runtime)
        limited_targets = con.execute(
            f"""
            SELECT *
            FROM read_parquet('{train_esc}')
            WHERE TRY_CAST(bet_id AS DOUBLE) IS NOT NULL
              AND payout_complete_dtm IS NOT NULL
              AND TRY_CAST(player_id AS BIGINT) IS NOT NULL
              AND strftime(CAST(payout_complete_dtm AS TIMESTAMPTZ), '%Y%m') = '{ym}'
            ORDER BY CAST(payout_complete_dtm AS TIMESTAMPTZ) ASC,
                     TRY_CAST(bet_id AS DOUBLE) ASC
            LIMIT {int(target_limit)}
            """,
        ).fetchdf()
    finally:
        con.close()
    limited_raw = root / "training_subset_raw.parquet"
    limited_targets.to_parquet(limited_raw, index=False)
    from trainer_hightier.trainer import _ensure_training_parquet_gaming_day_event_column

    subset_train = _ensure_training_parquet_gaming_day_event_column(
        limited_raw,
        duckdb_runtime=duckdb_runtime,
        cleaned_bet_parquet=Path(cleaned_bet_parquet).resolve(),
    )
    replay_out = root / "replay_prototype.parquet"
    oracle_out = root / "bounded_oracle.parquet"
    t0 = time.perf_counter()
    _, replay_metrics = materialize_short_term_replay_prototype(
        cleaned_bet_parquet=cleaned_bet_parquet,
        training_parquet_for_bet_ids=subset_train,
        out_parquet=replay_out,
        payout_yyyymm=payout_yyyymm,
        duckdb_runtime=duckdb_runtime,
        canonical_mapping_parquet=canonical_mapping_parquet,
        target_limit=None,
    )
    replay_elapsed = round(time.perf_counter() - t0, 6)
    t1 = time.perf_counter()
    materialize_fe_derived_short_term_parquet(
        cleaned_bet_parquet=cleaned_bet_parquet,
        training_parquet_for_bet_ids=subset_train,
        out_parquet=oracle_out,
        duckdb_runtime=duckdb_runtime,
        canonical_mapping_parquet=canonical_mapping_parquet,
        short_term_columns=(
            "fe__bets_cnt__w15m",
            "fe__wager_sum__w15m",
            "fe__time_since_last_bet_sec",
            "fe__odds__payout_odds_step_ratio",
        ),
        trial_columns=("bet__bets_cnt__w1h",),
        payout_yyyymm=payout_yyyymm,
    )
    bounded_elapsed = round(time.perf_counter() - t1, 6)
    replay_df = pq.read_table(replay_out).to_pandas()
    oracle_df = pq.read_table(oracle_out).to_pandas()
    parity = compare_replay_to_oracle(replay_df, oracle_df)
    speedup = round(bounded_elapsed / replay_elapsed, 3) if replay_elapsed > 0 else None
    go_no_go = evaluate_replay_go_no_go(
        parity_passed=bool(parity["passed"]),
        replay_elapsed_seconds=replay_elapsed,
        bounded_elapsed_seconds=bounded_elapsed,
    )
    return {
        "replay_elapsed_seconds": replay_elapsed,
        "bounded_elapsed_seconds": bounded_elapsed,
        "speedup_ratio": speedup,
        "parity": parity,
        "go_no_go": go_no_go,
        "replay_metrics": replay_metrics,
        "target_limit": int(target_limit),
        "payout_yyyymm": str(payout_yyyymm),
    }
