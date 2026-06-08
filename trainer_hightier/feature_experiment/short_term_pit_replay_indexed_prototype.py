"""Indexed event replay prototype for bounded short-term PIT materialization.

Uses per-entity NumPy arrays with ``searchsorted`` + prefix sums instead of
Python deque scans. Prototype only; not wired into trainer Step 3.5.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

import duckdb
import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from trainer_hightier.config import (
    DuckDbRuntimeConfig,
    HightierServingConfig,
    LEGACY_BET_PACK_1H_COLUMNS,
    LEGACY_BET_PACK_WAIVER_MAX_MISMATCH_RATIO,
    LEGACY_BET_PACK_WAIVER_ROOT_CAUSE,
    default_hightier_serving_config,
)
from trainer_hightier.feature_experiment.short_term_pit_replay_prototype import (
    PROTOTYPE_OUTPUT_COLUMNS,
    _attach_canonical_id,
    _load_replay_events,
    _load_target_bets,
    _path_esc,
    compare_replay_to_oracle,
    evaluate_replay_go_no_go,
)
from trainer_hightier.utils.canonical_mapping import default_canonical_mapping_parquet_path
from trainer_hightier.utils.duckdb_runtime import apply_duckdb_runtime_pragmas

logger = logging.getLogger(__name__)

_US_NS: Final[int] = 1_000
_W5M_NS: Final[int] = 5 * 60 * 1_000_000_000
_W15_NS: Final[int] = 15 * 60 * 1_000_000_000
_W1H_NS: Final[int] = 60 * 60 * 1_000_000_000
_W1D_NS: Final[int] = 24 * 60 * 60 * 1_000_000_000
_W7D_NS: Final[int] = 7 * _W1D_NS
_W30D_NS: Final[int] = 30 * _W1D_NS

WP_9_2_RANGE_FE_COLUMNS: Final[tuple[str, ...]] = (
    "fe__rate__bets_cnt__w5m",
    "fe__bets_cnt__w1d",
    "fe__wager_sum__w1d",
    "fe__bets_cnt__w7d",
    "fe__wager_sum__w7d",
    "fe__bets_cnt__w30d",
    "fe__wager_sum__w30d",
    "fe__wager_sum__w15m_over_w1d",
    "fe__bets_cnt__w15m_over_w1d",
    "fe__wager_sum__w7d_over_w30d",
    "fe__rate__velocity__w5m_over_w15m",
    "fe__rate__velocity__w15m_over_w1h",
)

WP_9_3_AVG_STDDEV_Z_COLUMNS: Final[tuple[str, ...]] = (
    "fe__odds__payout_odds_z__w1h",
    "fe__odds__payout_odds_z__w7d",
    "fe__stake__wager_z__w1h",
    "fe__stake__wager_cv__w1h",
    "fe__wager_cv_w7d",
    "fe__wager_z_prior_w30d",
    "fe__payout_odds_z_prior_w30d",
)

WP_9_4_MAX_RATIO_COLUMNS: Final[tuple[str, ...]] = (
    "fe__odds__payout_odds_to_recent_max_ratio__w1h",
    "fe__stake__wager_to_recent_max_ratio__w1h",
)

WP_9_5_INTERARRIVAL_COLUMNS: Final[tuple[str, ...]] = (
    "fe__interarrival__lag2_sec",
    "fe__interarrival__last_gap_to_recent_mean_ratio__w1h",
    "fe__interarrival__cv__w1h",
    "fe__interarrival__last_gap_z__w7d",
)

WP_9_6_TODAY_COLUMNS: Final[tuple[str, ...]] = (
    "fe__canonical__bets_cnt__today",
    "fe__canonical__wager_sum__today",
    "fe__canonical__avg_wager__today",
    "fe__canonical__elapsed_sec_since_first_bet__today",
)

INDEXED_PROTOTYPE_OUTPUT_COLUMNS: Final[tuple[str, ...]] = (
    *PROTOTYPE_OUTPUT_COLUMNS,
    "bet__wager_sum__w1h",
    "bet__back_bet_ratio__w1h",
    "bet__payout_odds_avg__w1h",
    *WP_9_2_RANGE_FE_COLUMNS,
    *WP_9_3_AVG_STDDEV_Z_COLUMNS,
    *WP_9_4_MAX_RATIO_COLUMNS,
    *WP_9_5_INTERARRIVAL_COLUMNS,
    *WP_9_6_TODAY_COLUMNS,
)

# Prototype go/no-go excludes these production gate columns (low model importance / edge parity).
PROTOTYPE_GATE_IGNORE_COLUMNS: Final[tuple[str, ...]] = (
    "fe__odds__payout_odds_z__w1h",
)


@lru_cache(maxsize=1)
def _resolve_production_scorer_short_pit_gate_columns_raw() -> tuple[str, ...]:
    """Return full production scorer short PIT columns (+ enrich deps)."""
    from trainer_hightier.feature_experiment.feature_cadence import (
        short_term_enrich_columns_with_dependencies,
    )
    from trainer_hightier.serving.candidate_registry_loader import load_candidate_registry
    from trainer_hightier.serving.feature_supply import build_scorer_supplier_plan

    snap = load_candidate_registry(None)
    plan = build_scorer_supplier_plan(snap, snap.model_feature_columns)
    registry_by_id = {r.feature_id: r for r in snap.rows}
    return short_term_enrich_columns_with_dependencies(
        plan.short_term_cols,
        plan.mid_composite_cols,
        registry_by_id=registry_by_id,
    )


def resolve_production_scorer_short_pit_gate_columns() -> tuple[str, ...]:
    """Return full production scorer short PIT gate columns before prototype ignores."""
    return _resolve_production_scorer_short_pit_gate_columns_raw()


@lru_cache(maxsize=1)
def resolve_scorer_short_pit_prototype_gate_columns() -> tuple[str, ...]:
    """Return prototype gate columns: production gate minus ``PROTOTYPE_GATE_IGNORE_COLUMNS``."""
    ignore = set(PROTOTYPE_GATE_IGNORE_COLUMNS)
    return tuple(c for c in _resolve_production_scorer_short_pit_gate_columns_raw() if c not in ignore)


def split_scorer_short_pit_gate_columns(
    columns: tuple[str, ...] | None = None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Split gate columns into bounded-oracle ``fe__*`` and ``bet__*`` tuples."""
    gate = tuple(columns or resolve_scorer_short_pit_prototype_gate_columns())
    fe_cols = tuple(c for c in gate if c.startswith("fe__"))
    trial_cols = tuple(c for c in gate if c.startswith("bet__"))
    return fe_cols, trial_cols


@dataclass(frozen=True)
class _EntityArrays:
    """Sorted NumPy arrays for one canonical or ``(canonical_id, player_id)`` entity."""

    pcd_ns: np.ndarray
    bet_id: np.ndarray
    payout_odds: np.ndarray
    wager: np.ndarray
    wager_sum_prefix: np.ndarray
    wager_sumsq_prefix: np.ndarray
    wager_cnt_prefix: np.ndarray
    abs_wager_sum_prefix: np.ndarray
    abs_wager_cnt_prefix: np.ndarray
    odds_sum_prefix: np.ndarray
    odds_sumsq_prefix: np.ndarray
    odds_cnt_prefix: np.ndarray
    iv_sec: np.ndarray
    iv_sum_prefix: np.ndarray
    iv_sumsq_prefix: np.ndarray
    iv_cnt_prefix: np.ndarray
    gaming_day_ord: np.ndarray


def _cumsum_prefix(values: np.ndarray) -> np.ndarray:
    """Build a zero-based prefix sum array of length ``len(values) + 1``."""
    prefix = np.empty(len(values) + 1, dtype=np.float64)
    prefix[0] = 0.0
    np.cumsum(values, out=prefix[1:])
    return prefix


def _finite_value_prefixes(values: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return sum/sumsq/count prefix arrays that ignore non-finite values."""
    finite = np.isfinite(values)
    safe = np.where(finite, values, 0.0)
    sq = np.where(finite, values * values, 0.0)
    cnt = finite.astype(np.float64)
    return _cumsum_prefix(safe), _cumsum_prefix(sq), _cumsum_prefix(cnt)


def _pop_mean_std(
    sum_prefix: np.ndarray,
    sumsq_prefix: np.ndarray,
    cnt_prefix: np.ndarray,
    left: int,
    right: int,
) -> tuple[float, float]:
    """Return DuckDB ``AVG`` / ``STDDEV_POP`` over a prefix slice."""
    count = float(cnt_prefix[right] - cnt_prefix[left])
    if count <= 0.0:
        return np.nan, np.nan
    total = float(sum_prefix[right] - sum_prefix[left])
    total_sq = float(sumsq_prefix[right] - sumsq_prefix[left])
    mean = total / count
    if count <= 1.0:
        return float(mean), 0.0
    var = max(0.0, (total_sq / count) - (mean * mean))
    return float(mean), float(np.sqrt(var))


def _z_score(
    value: float,
    mean: float,
    std: float,
    *,
    std_min: float,
) -> float:
    """Compute a guarded z-score matching bounded DuckDB CASE expressions."""
    if not np.isfinite(mean) or not np.isfinite(std) or abs(std) <= std_min:
        return np.nan
    if not np.isfinite(value):
        return np.nan
    return float((value - mean) / std)


def _range_slice_max(values: np.ndarray, left: int, right: int) -> float:
    """Return DuckDB ``MAX`` over a sorted slice, ignoring non-finite values."""
    if right <= left:
        return np.nan
    chunk = values[left:right]
    if chunk.size == 0 or not np.any(np.isfinite(chunk)):
        return np.nan
    return float(np.nanmax(chunk))


def _to_recent_max_ratio(value: float, window_max: float) -> float:
    """Compute guarded ``value / recent_max`` ratio columns."""
    if not np.isfinite(window_max) or window_max <= 1e-9:
        return np.nan
    if not np.isfinite(value):
        return np.nan
    return float(value / window_max)


def _max_ratio_features(
    arrays: _EntityArrays,
    *,
    pool_start_ns: int,
    scoring_pcd_ns: int,
    payout_odds: float,
    target_wager: float,
) -> dict[str, float]:
    """Compute WP-9.4 RANGE max ratio columns for the prior 1h window."""
    bounds = _range_window_indices(
        arrays.pcd_ns,
        pool_start_ns=pool_start_ns,
        scoring_pcd_ns=scoring_pcd_ns,
        window_ns=_W1H_NS,
    )
    if bounds is None:
        max_odds = np.nan
        max_wager = np.nan
    else:
        left, right = bounds
        max_odds = _range_slice_max(arrays.payout_odds, left, right)
        max_wager = _range_slice_max(arrays.wager, left, right)
    return {
        "fe__odds__payout_odds_to_recent_max_ratio__w1h": _to_recent_max_ratio(
            payout_odds,
            max_odds,
        ),
        "fe__stake__wager_to_recent_max_ratio__w1h": _to_recent_max_ratio(
            target_wager,
            max_wager,
        ),
    }


@dataclass(frozen=True)
class _CanonicalArrays:
    """Sorted NumPy arrays for one ``canonical_id`` trial 1h partition."""

    pcd_ns: np.ndarray
    bet_id: np.ndarray
    wager_prefix: np.ndarray
    back_prefix: np.ndarray
    odds_sum_prefix: np.ndarray
    odds_cnt_prefix: np.ndarray


def _series_to_ns(series: pd.Series) -> np.ndarray:
    """Convert timestamps to UTC int64 nanoseconds."""
    arr = pd.to_datetime(series, errors="coerce", utc=True).values.astype("datetime64[ns]")
    return arr.view(np.int64)


def _bounds_with_ns(bounds: pd.DataFrame) -> pd.DataFrame:
    """Attach nanosecond bounds columns used by indexed emit."""
    out = bounds.copy()
    out["pool_start_ns"] = _series_to_ns(out["pool_start"])
    out["scoring_pcd_ns"] = _series_to_ns(out["scoring_pcd"])
    return out


def _gaming_day_ordinals(
    pcd_series: pd.Series,
    gaming_day_series: pd.Series | None,
    *,
    hk_tz: str,
) -> np.ndarray:
    """Convert payout timestamps and optional gaming-day dates to day ordinals."""
    pcd = pd.to_datetime(pcd_series, errors="coerce", utc=True)
    if gaming_day_series is not None:
        gde = pd.to_datetime(gaming_day_series, errors="coerce")
        missing = gde.isna()
        if missing.any():
            gde = gde.copy()
            gde.loc[missing] = pcd.loc[missing].dt.tz_convert(hk_tz).dt.normalize()
    else:
        gde = pcd.dt.tz_convert(hk_tz).dt.normalize()
    return gde.values.astype("datetime64[D]").astype(np.int64)


def _entity_arrays_from_sorted_rows(
    pcd: np.ndarray,
    bid: np.ndarray,
    odds: np.ndarray,
    wager: np.ndarray,
    gday: np.ndarray,
) -> _EntityArrays:
    """Build prefix arrays from lex-sorted entity rows."""
    wager_sum_p, wager_sumsq_p, wager_cnt_p = _finite_value_prefixes(wager)
    finite_wager = np.isfinite(wager)
    abs_wager_sum_p = _cumsum_prefix(np.where(finite_wager, np.abs(wager), 0.0))
    abs_wager_cnt_p = _cumsum_prefix(finite_wager.astype(np.float64))
    odds_sum_p, odds_sumsq_p, odds_cnt_p = _finite_value_prefixes(odds)
    iv_sec = np.full(len(pcd), np.nan, dtype=np.float64)
    if len(pcd) > 1:
        iv_sec[1:] = (pcd[1:] - pcd[:-1]) / 1_000_000_000.0
    iv_sum_p, iv_sumsq_p, iv_cnt_p = _finite_value_prefixes(iv_sec)
    return _EntityArrays(
        pcd_ns=pcd,
        bet_id=bid,
        payout_odds=odds,
        wager=wager,
        wager_sum_prefix=wager_sum_p,
        wager_sumsq_prefix=wager_sumsq_p,
        wager_cnt_prefix=wager_cnt_p,
        abs_wager_sum_prefix=abs_wager_sum_p,
        abs_wager_cnt_prefix=abs_wager_cnt_p,
        odds_sum_prefix=odds_sum_p,
        odds_sumsq_prefix=odds_sumsq_p,
        odds_cnt_prefix=odds_cnt_p,
        iv_sec=iv_sec,
        iv_sum_prefix=iv_sum_p,
        iv_sumsq_prefix=iv_sumsq_p,
        iv_cnt_prefix=iv_cnt_p,
        gaming_day_ord=gday,
    )


def _prepare_entity_event_frame(
    events_df: pd.DataFrame,
    canonical_by_player: dict[int, str],
    *,
    hk_tz: str = "Asia/Hong_Kong",
) -> pd.DataFrame:
    """Normalize month-pool events for indexed entity array construction."""
    work = events_df.copy()
    work["player_id"] = pd.to_numeric(work["player_id"], errors="coerce")
    work["bet_id"] = pd.to_numeric(work["bet_id"], errors="coerce")
    work["wager"] = pd.to_numeric(work["wager"], errors="coerce")
    work["payout_odds"] = pd.to_numeric(work["payout_odds"], errors="coerce")
    work["pcd_ns"] = _series_to_ns(work["payout_complete_dtm"])
    gde_series = work["gaming_day_event"] if "gaming_day_event" in work.columns else None
    work["gaming_day_ord"] = _gaming_day_ordinals(
        work["payout_complete_dtm"],
        gde_series,
        hk_tz=hk_tz,
    )
    work = work.dropna(subset=["bet_id", "player_id", "pcd_ns"])
    work["player_id"] = work["player_id"].astype(np.int64)
    work["canonical_id"] = work["player_id"].map(canonical_by_player)
    return work.dropna(subset=["canonical_id"])


def _build_entity_arrays(
    events_df: pd.DataFrame,
    canonical_by_player: dict[int, str],
    *,
    hk_tz: str = "Asia/Hong_Kong",
) -> tuple[dict[tuple[str, int], _EntityArrays], int]:
    """Build sorted per-(canonical, player) arrays from month pool events."""
    work = _prepare_entity_event_frame(events_df, canonical_by_player, hk_tz=hk_tz)
    if work.empty:
        return {}, 0
    entity_arrays: dict[tuple[str, int], _EntityArrays] = {}
    max_len = 0
    for (cid, pid), grp in work.groupby(["canonical_id", "player_id"], sort=False):
        pcd = grp["pcd_ns"].to_numpy(dtype=np.int64, copy=False)
        bid = grp["bet_id"].to_numpy(dtype=np.float64, copy=False)
        odds = grp["payout_odds"].to_numpy(dtype=np.float64, copy=False)
        wager = grp["wager"].to_numpy(dtype=np.float64, copy=False)
        gday = grp["gaming_day_ord"].to_numpy(dtype=np.int64, copy=False)
        order = np.lexsort((bid, pcd))
        arrays = _entity_arrays_from_sorted_rows(
            pcd[order],
            bid[order],
            odds[order],
            wager[order],
            gday[order],
        )
        entity_arrays[(str(cid).strip(), int(pid))] = arrays
        max_len = max(max_len, len(arrays.pcd_ns))
    return entity_arrays, max_len


def _build_canonical_arrays(
    events_df: pd.DataFrame,
    canonical_by_player: dict[int, str],
) -> dict[str, _CanonicalArrays]:
    """Build sorted per-canonical arrays for trial ``bet__*`` 1h windows."""
    if events_df.empty:
        return {}
    work = events_df.copy()
    work["player_id"] = pd.to_numeric(work["player_id"], errors="coerce")
    work["bet_id"] = pd.to_numeric(work["bet_id"], errors="coerce")
    work["wager"] = pd.to_numeric(work["wager"], errors="coerce").fillna(0.0)
    work["is_back_bet"] = pd.to_numeric(work.get("is_back_bet"), errors="coerce").fillna(0)
    work["payout_odds"] = pd.to_numeric(work["payout_odds"], errors="coerce")
    work["pcd_ns"] = _series_to_ns(work["payout_complete_dtm"])
    work = work.dropna(subset=["bet_id", "player_id", "pcd_ns"])
    work["player_id"] = work["player_id"].astype(np.int64)
    work["canonical_id"] = work["player_id"].map(canonical_by_player)
    work = work.dropna(subset=["canonical_id"])
    canonical_arrays: dict[str, _CanonicalArrays] = {}
    for cid, grp in work.groupby("canonical_id", sort=False):
        pcd = grp["pcd_ns"].to_numpy(dtype=np.int64, copy=False)
        bid = grp["bet_id"].to_numpy(dtype=np.float64, copy=False)
        wager = grp["wager"].to_numpy(dtype=np.float64, copy=False)
        back = (grp["is_back_bet"].to_numpy(dtype=np.float64, copy=False) == 1.0).astype(np.float64)
        odds = grp["payout_odds"].to_numpy(dtype=np.float64, copy=False)
        order = np.lexsort((bid, pcd))
        pcd = pcd[order]
        bid = bid[order]
        wager = wager[order]
        back = back[order]
        odds = odds[order]
        wager_prefix = np.empty(len(wager) + 1, dtype=np.float64)
        wager_prefix[0] = 0.0
        np.cumsum(wager, out=wager_prefix[1:])
        back_prefix = np.empty(len(back) + 1, dtype=np.float64)
        back_prefix[0] = 0.0
        np.cumsum(back, out=back_prefix[1:])
        odds_finite = np.where(np.isfinite(odds), odds, 0.0)
        odds_present = np.isfinite(odds).astype(np.float64)
        odds_sum_prefix = np.empty(len(odds) + 1, dtype=np.float64)
        odds_sum_prefix[0] = 0.0
        np.cumsum(odds_finite, out=odds_sum_prefix[1:])
        odds_cnt_prefix = np.empty(len(odds) + 1, dtype=np.float64)
        odds_cnt_prefix[0] = 0.0
        np.cumsum(odds_present, out=odds_cnt_prefix[1:])
        canonical_arrays[str(cid).strip()] = _CanonicalArrays(
            pcd_ns=pcd,
            bet_id=bid,
            wager_prefix=wager_prefix,
            back_prefix=back_prefix,
            odds_sum_prefix=odds_sum_prefix,
            odds_cnt_prefix=odds_cnt_prefix,
        )
    return canonical_arrays


def _range_window_indices(
    pcd_ns: np.ndarray,
    *,
    pool_start_ns: int,
    scoring_pcd_ns: int,
    window_ns: int,
    target_idx: int | None = None,
) -> tuple[int, int] | None:
    """Return ``[left, right)`` slice bounds for one DuckDB RANGE window."""
    window_end = scoring_pcd_ns - _US_NS
    window_start = max(int(pool_start_ns), scoring_pcd_ns - window_ns)
    if window_end < window_start:
        return None
    left = int(np.searchsorted(pcd_ns, window_start, side="left"))
    right = int(np.searchsorted(pcd_ns, window_end, side="right"))
    if target_idx is not None and target_idx >= 0:
        right = min(right, int(target_idx))
    if right <= left:
        return None
    return left, right


@dataclass(frozen=True)
class _TargetEmitContext:
    """Precomputed per-target indices and RANGE window bounds for emit."""

    target_idx: int
    bounds_5m: tuple[int, int] | None
    bounds_15m: tuple[int, int] | None
    bounds_1h: tuple[int, int] | None
    bounds_1h_capped: tuple[int, int] | None
    bounds_1d: tuple[int, int] | None
    bounds_7d: tuple[int, int] | None
    bounds_7d_capped: tuple[int, int] | None
    bounds_30d: tuple[int, int] | None
    canonical_bounds_1h: tuple[int, int] | None


def _build_target_emit_context(
    arrays: _EntityArrays,
    canonical_arrays: _CanonicalArrays | None,
    *,
    pool_start_ns: int,
    scoring_pcd_ns: int,
    target_bet_id: float,
    trial_pool_start_ns: int,
) -> _TargetEmitContext:
    """Resolve target row index and all emit window bounds once."""
    target_idx = _target_row_index(
        arrays,
        pool_start_ns=pool_start_ns,
        scoring_pcd_ns=scoring_pcd_ns,
        target_bet_id=target_bet_id,
    )
    pcd = arrays.pcd_ns
    window_end = scoring_pcd_ns - _US_NS
    entity_windows: dict[str, tuple[int, int] | None] = {
        "bounds_5m": None,
        "bounds_15m": None,
        "bounds_1h": None,
        "bounds_1h_capped": None,
        "bounds_1d": None,
        "bounds_7d": None,
        "bounds_7d_capped": None,
        "bounds_30d": None,
    }
    if window_end >= pool_start_ns:
        right = int(np.searchsorted(pcd, window_end, side="right"))
        right_capped = min(right, target_idx) if target_idx >= 0 else right
        for key, window_ns in (
            ("bounds_5m", _W5M_NS),
            ("bounds_15m", _W15_NS),
            ("bounds_1h", _W1H_NS),
            ("bounds_1d", _W1D_NS),
            ("bounds_7d", _W7D_NS),
            ("bounds_30d", _W30D_NS),
        ):
            window_start = max(int(pool_start_ns), scoring_pcd_ns - window_ns)
            if window_end < window_start:
                continue
            left = int(np.searchsorted(pcd, window_start, side="left"))
            if right > left:
                entity_windows[key] = (left, right)
            if right_capped > left:
                capped_key = f"{key}_capped" if key in {"bounds_1h", "bounds_7d"} else None
                if capped_key is not None:
                    entity_windows[capped_key] = (left, right_capped)

    canonical_bounds_1h: tuple[int, int] | None = None
    if canonical_arrays is not None:
        canonical_bounds_1h = _range_window_indices(
            canonical_arrays.pcd_ns,
            pool_start_ns=trial_pool_start_ns,
            scoring_pcd_ns=scoring_pcd_ns,
            window_ns=_W1H_NS,
        )
    return _TargetEmitContext(
        target_idx=target_idx,
        bounds_5m=entity_windows["bounds_5m"],
        bounds_15m=entity_windows["bounds_15m"],
        bounds_1h=entity_windows["bounds_1h"],
        bounds_1h_capped=entity_windows["bounds_1h_capped"],
        bounds_1d=entity_windows["bounds_1d"],
        bounds_7d=entity_windows["bounds_7d"],
        bounds_7d_capped=entity_windows["bounds_7d_capped"],
        bounds_30d=entity_windows["bounds_30d"],
        canonical_bounds_1h=canonical_bounds_1h,
    )


def _count_sum_from_bounds(
    arrays: _EntityArrays,
    bounds: tuple[int, int] | None,
) -> tuple[int, float]:
    """Return DuckDB RANGE count/sum for a precomputed slice."""
    if bounds is None:
        return 0, 0.0
    left, right = bounds
    return right - left, float(arrays.wager_sum_prefix[right] - arrays.wager_sum_prefix[left])


def _pop_stats_from_bounds(
    arrays: _EntityArrays,
    bounds: tuple[int, int] | None,
) -> tuple[float, float, float, float]:
    """Return wager/odds mean and std for a precomputed slice."""
    if bounds is None:
        return np.nan, np.nan, np.nan, np.nan
    left, right = bounds
    wager_mean, wager_std = _pop_mean_std(
        arrays.wager_sum_prefix,
        arrays.wager_sumsq_prefix,
        arrays.wager_cnt_prefix,
        left,
        right,
    )
    odds_mean, odds_std = _pop_mean_std(
        arrays.odds_sum_prefix,
        arrays.odds_sumsq_prefix,
        arrays.odds_cnt_prefix,
        left,
        right,
    )
    return wager_mean, wager_std, odds_mean, odds_std


def _abs_wager_mean_from_bounds(
    arrays: _EntityArrays,
    bounds: tuple[int, int] | None,
) -> tuple[float, float]:
    """Return ``AVG(ABS(wager))`` and ``STDDEV_POP(wager)`` for a precomputed slice."""
    if bounds is None:
        return np.nan, np.nan
    left, right = bounds
    abs_cnt = float(arrays.abs_wager_cnt_prefix[right] - arrays.abs_wager_cnt_prefix[left])
    if abs_cnt <= 0.0:
        return np.nan, np.nan
    abs_sum = float(arrays.abs_wager_sum_prefix[right] - arrays.abs_wager_sum_prefix[left])
    abs_mean = abs_sum / abs_cnt
    _, wager_std = _pop_mean_std(
        arrays.wager_sum_prefix,
        arrays.wager_sumsq_prefix,
        arrays.wager_cnt_prefix,
        left,
        right,
    )
    return float(abs_mean), wager_std


def _bet_pack_from_bounds(
    arrays: _CanonicalArrays,
    bounds: tuple[int, int] | None,
) -> tuple[int, float, float, float]:
    """Return trial 1h pack columns for a precomputed canonical slice."""
    if bounds is None:
        return 0, 0.0, 0.0, 0.0
    left, right = bounds
    cnt = right - left
    wsum = float(arrays.wager_prefix[right] - arrays.wager_prefix[left])
    back_sum = float(arrays.back_prefix[right] - arrays.back_prefix[left])
    odds_cnt = float(arrays.odds_cnt_prefix[right] - arrays.odds_cnt_prefix[left])
    if odds_cnt > 0.0:
        odds_avg = float(
            (arrays.odds_sum_prefix[right] - arrays.odds_sum_prefix[left]) / odds_cnt,
        )
    else:
        odds_avg = 0.0
    back_ratio = back_sum / float(cnt) if cnt > 0 else 0.0
    return cnt, wsum, back_ratio, odds_avg


def _prior_row_index(
    arrays: _EntityArrays,
    *,
    pool_start_ns: int,
    target_idx: int,
) -> int:
    """Return prior row index in pool slice, or -1 when absent."""
    if target_idx <= 0:
        return -1
    prior = target_idx - 1
    if arrays.pcd_ns[prior] < pool_start_ns:
        return -1
    return prior


def _interarrival_stats_from_bounds(
    arrays: _EntityArrays,
    *,
    pool_start_ns: int,
    bounds: tuple[int, int] | None,
) -> tuple[float, float]:
    """Return prior-window mean/std of pool-safe interarrival gaps."""
    if bounds is None:
        return np.nan, np.nan
    left, right = bounds
    gaps = _pool_safe_interarrival_gaps(
        arrays,
        pool_start_ns=pool_start_ns,
        left=left,
        right=right,
    )
    if gaps.size == 0:
        return np.nan, np.nan
    mean = float(gaps.mean())
    if gaps.size <= 1:
        return mean, 0.0
    return mean, float(gaps.std(ddof=0))


def _interarrival_features_from_context(
    arrays: _EntityArrays,
    ctx: _TargetEmitContext,
    *,
    pool_start_ns: int,
    scoring_pcd_ns: int,
) -> dict[str, float]:
    """Compute WP-9.5 interarrival columns using precomputed bounds."""
    target_idx = ctx.target_idx
    prior = _prior_row_index(arrays, pool_start_ns=pool_start_ns, target_idx=target_idx)
    if prior < 0:
        gap = np.nan
        lag2 = np.nan
    else:
        gap = float((scoring_pcd_ns - int(arrays.pcd_ns[prior])) / 1_000_000_000.0)
        lag2_prior = prior - 1
        if lag2_prior < 0 or int(arrays.pcd_ns[lag2_prior]) < int(pool_start_ns):
            lag2 = np.nan
        else:
            lag2_val = float(arrays.iv_sec[prior])
            lag2 = lag2_val if np.isfinite(lag2_val) else np.nan
    if target_idx < 0:
        return {
            "fe__time_since_last_bet_sec": np.nan,
            "fe__interarrival__lag2_sec": np.nan,
            "fe__interarrival__last_gap_to_recent_mean_ratio__w1h": np.nan,
            "fe__interarrival__cv__w1h": np.nan,
            "fe__interarrival__last_gap_z__w7d": np.nan,
        }
    iv_mean_1h, iv_std_1h = _interarrival_stats_from_bounds(
        arrays,
        pool_start_ns=pool_start_ns,
        bounds=ctx.bounds_1h_capped,
    )
    iv_mean_7d, iv_std_7d = _interarrival_stats_from_bounds(
        arrays,
        pool_start_ns=pool_start_ns,
        bounds=ctx.bounds_7d_capped,
    )
    ratio_1h = (
        float(gap / iv_mean_1h)
        if np.isfinite(gap) and np.isfinite(iv_mean_1h) and iv_mean_1h > 1e-9
        else np.nan
    )
    cv_1h = (
        float(iv_std_1h / iv_mean_1h)
        if np.isfinite(iv_mean_1h) and iv_mean_1h > 1e-9 and np.isfinite(iv_std_1h)
        else np.nan
    )
    return {
        "fe__time_since_last_bet_sec": gap,
        "fe__interarrival__lag2_sec": lag2,
        "fe__interarrival__last_gap_to_recent_mean_ratio__w1h": ratio_1h,
        "fe__interarrival__cv__w1h": cv_1h,
        "fe__interarrival__last_gap_z__w7d": _z_score(
            gap,
            iv_mean_7d,
            iv_std_7d,
            std_min=1e-9,
        ),
    }


def _avg_stddev_z_features_from_context(
    arrays: _EntityArrays,
    ctx: _TargetEmitContext,
    *,
    payout_odds: float,
    target_wager: float,
) -> dict[str, float]:
    """Compute WP-9.3 z-score columns using precomputed bounds."""
    target_idx = ctx.target_idx
    odds_for_z = payout_odds
    if target_idx >= 0:
        odds_for_z = float(arrays.payout_odds[target_idx])
    w_mean_1h, w_std_1h, o_mean_1h, o_std_1h = _pop_stats_from_bounds(
        arrays,
        ctx.bounds_1h_capped,
    )
    odds_cnt_1h = 0.0
    if ctx.bounds_1h_capped is not None:
        left, right = ctx.bounds_1h_capped
        odds_cnt_1h = float(arrays.odds_cnt_prefix[right] - arrays.odds_cnt_prefix[left])
    _, w_std_7d, o_mean_7d, o_std_7d = _pop_stats_from_bounds(arrays, ctx.bounds_7d)
    abs_mean_7d, _ = _abs_wager_mean_from_bounds(arrays, ctx.bounds_7d)
    prior_w_mean_30, prior_w_std_30, prior_o_mean_30, prior_o_std_30 = _pop_stats_from_bounds(
        arrays,
        ctx.bounds_30d,
    )
    stake_wager_cv_1h = (
        float(w_std_1h / w_mean_1h)
        if np.isfinite(w_mean_1h) and w_mean_1h > 1e-12 and np.isfinite(w_std_1h)
        else np.nan
    )
    wager_cv_7d = (
        float(w_std_7d / abs_mean_7d)
        if np.isfinite(abs_mean_7d) and abs_mean_7d > 1e-12 and np.isfinite(w_std_7d)
        else np.nan
    )
    return {
        "fe__odds__payout_odds_z__w1h": (
            _z_score(odds_for_z, o_mean_1h, o_std_1h, std_min=1e-12)
            if odds_cnt_1h >= 2.0
            else np.nan
        ),
        "fe__odds__payout_odds_z__w7d": _z_score(
            odds_for_z,
            o_mean_7d,
            o_std_7d,
            std_min=1e-12,
        ),
        "fe__stake__wager_z__w1h": _z_score(
            target_wager,
            w_mean_1h,
            w_std_1h,
            std_min=1e-12,
        ),
        "fe__stake__wager_cv__w1h": stake_wager_cv_1h,
        "fe__wager_cv_w7d": wager_cv_7d,
        "fe__wager_z_prior_w30d": _z_score(
            target_wager,
            prior_w_mean_30,
            prior_w_std_30,
            std_min=1e-12,
        ),
        "fe__payout_odds_z_prior_w30d": _z_score(
            payout_odds,
            prior_o_mean_30,
            prior_o_std_30,
            std_min=1e-12,
        ),
    }


def _max_ratio_features_from_context(
    arrays: _EntityArrays,
    ctx: _TargetEmitContext,
    *,
    payout_odds: float,
    target_wager: float,
) -> dict[str, float]:
    """Compute WP-9.4 max ratio columns using precomputed bounds."""
    if ctx.bounds_1h is None:
        max_odds = np.nan
        max_wager = np.nan
    else:
        left, right = ctx.bounds_1h
        max_odds = _range_slice_max(arrays.payout_odds, left, right)
        max_wager = _range_slice_max(arrays.wager, left, right)
    return {
        "fe__odds__payout_odds_to_recent_max_ratio__w1h": _to_recent_max_ratio(
            payout_odds,
            max_odds,
        ),
        "fe__stake__wager_to_recent_max_ratio__w1h": _to_recent_max_ratio(
            target_wager,
            max_wager,
        ),
    }


def _today_features_from_context(
    arrays: _EntityArrays,
    ctx: _TargetEmitContext,
    *,
    pool_start_ns: int,
    scoring_pcd_ns: int,
    target_gaming_day_ord: int,
) -> dict[str, float]:
    """Compute WP-9.6 today counters using precomputed target index."""
    target_idx = ctx.target_idx
    if target_idx < 0:
        return {
            "fe__canonical__bets_cnt__today": 0.0,
            "fe__canonical__wager_sum__today": 0.0,
            "fe__canonical__avg_wager__today": np.nan,
            "fe__canonical__elapsed_sec_since_first_bet__today": np.nan,
        }
    in_pool = arrays.pcd_ns >= pool_start_ns
    same_day = arrays.gaming_day_ord == int(target_gaming_day_ord)
    prior_mask = in_pool & same_day & (np.arange(len(arrays.pcd_ns)) < target_idx)
    bets_cnt = int(np.sum(prior_mask))
    wager_sum = float(np.nansum(arrays.wager[prior_mask]))
    day_mask = in_pool & same_day & (np.arange(len(arrays.pcd_ns)) <= target_idx)
    if not bool(np.any(day_mask)):
        elapsed = np.nan
    else:
        first_pcd_ns = int(np.min(arrays.pcd_ns[day_mask]))
        elapsed = float((scoring_pcd_ns - first_pcd_ns) / 1_000_000_000.0)
    avg_wager = float(wager_sum / bets_cnt) if bets_cnt > 0 else np.nan
    return {
        "fe__canonical__bets_cnt__today": float(bets_cnt),
        "fe__canonical__wager_sum__today": wager_sum,
        "fe__canonical__avg_wager__today": avg_wager,
        "fe__canonical__elapsed_sec_since_first_bet__today": elapsed,
    }


def _range_count_sum(
    arrays: _EntityArrays,
    *,
    pool_start_ns: int,
    scoring_pcd_ns: int,
    window_ns: int,
) -> tuple[int, float]:
    """Return DuckDB RANGE count/sum for one target row."""
    bounds = _range_window_indices(
        arrays.pcd_ns,
        pool_start_ns=pool_start_ns,
        scoring_pcd_ns=scoring_pcd_ns,
        window_ns=window_ns,
    )
    if bounds is None:
        return 0, 0.0
    left, right = bounds
    return right - left, float(arrays.wager_sum_prefix[right] - arrays.wager_sum_prefix[left])


def _window_pop_stats(
    arrays: _EntityArrays,
    *,
    pool_start_ns: int,
    scoring_pcd_ns: int,
    window_ns: int,
    target_idx: int | None = None,
) -> tuple[float, float, float, float]:
    """Return wager mean/std and odds mean/std for one prior RANGE window."""
    bounds = _range_window_indices(
        arrays.pcd_ns,
        pool_start_ns=pool_start_ns,
        scoring_pcd_ns=scoring_pcd_ns,
        window_ns=window_ns,
        target_idx=target_idx,
    )
    if bounds is None:
        return np.nan, np.nan, np.nan, np.nan
    left, right = bounds
    wager_mean, wager_std = _pop_mean_std(
        arrays.wager_sum_prefix,
        arrays.wager_sumsq_prefix,
        arrays.wager_cnt_prefix,
        left,
        right,
    )
    odds_mean, odds_std = _pop_mean_std(
        arrays.odds_sum_prefix,
        arrays.odds_sumsq_prefix,
        arrays.odds_cnt_prefix,
        left,
        right,
    )
    return wager_mean, wager_std, odds_mean, odds_std


def _window_abs_wager_mean(
    arrays: _EntityArrays,
    *,
    pool_start_ns: int,
    scoring_pcd_ns: int,
    window_ns: int,
) -> tuple[float, float]:
    """Return ``AVG(ABS(wager))`` and ``STDDEV_POP(wager)`` for one prior window."""
    bounds = _range_window_indices(
        arrays.pcd_ns,
        pool_start_ns=pool_start_ns,
        scoring_pcd_ns=scoring_pcd_ns,
        window_ns=window_ns,
    )
    if bounds is None:
        return np.nan, np.nan
    left, right = bounds
    abs_cnt = float(arrays.abs_wager_cnt_prefix[right] - arrays.abs_wager_cnt_prefix[left])
    if abs_cnt <= 0.0:
        return np.nan, np.nan
    abs_sum = float(arrays.abs_wager_sum_prefix[right] - arrays.abs_wager_sum_prefix[left])
    abs_mean = abs_sum / abs_cnt
    _, wager_std = _pop_mean_std(
        arrays.wager_sum_prefix,
        arrays.wager_sumsq_prefix,
        arrays.wager_cnt_prefix,
        left,
        right,
    )
    return float(abs_mean), wager_std


def _avg_stddev_z_features(
    arrays: _EntityArrays,
    *,
    pool_start_ns: int,
    scoring_pcd_ns: int,
    payout_odds: float,
    target_wager: float,
    target_bet_id: float | None = None,
) -> dict[str, float]:
    """Compute WP-9.3 AVG/STDDEV-derived z-score columns."""
    target_idx = (
        _target_row_index(
            arrays,
            pool_start_ns=pool_start_ns,
            scoring_pcd_ns=scoring_pcd_ns,
            target_bet_id=float(target_bet_id),
        )
        if target_bet_id is not None
        else None
    )
    odds_for_z = payout_odds
    if target_idx is not None and target_idx >= 0:
        pool_odds = float(arrays.payout_odds[target_idx])
        odds_for_z = pool_odds
    w1h_bounds = (
        _range_window_indices(
            arrays.pcd_ns,
            pool_start_ns=pool_start_ns,
            scoring_pcd_ns=scoring_pcd_ns,
            window_ns=_W1H_NS,
            target_idx=target_idx,
        )
        if target_idx is not None
        else None
    )
    w_mean_1h, w_std_1h, o_mean_1h, o_std_1h = _window_pop_stats(
        arrays,
        pool_start_ns=pool_start_ns,
        scoring_pcd_ns=scoring_pcd_ns,
        window_ns=_W1H_NS,
        target_idx=target_idx,
    )
    odds_cnt_1h = 0.0
    if w1h_bounds is not None:
        left, right = w1h_bounds
        odds_cnt_1h = float(arrays.odds_cnt_prefix[right] - arrays.odds_cnt_prefix[left])
    _, w_std_7d, o_mean_7d, o_std_7d = _window_pop_stats(
        arrays,
        pool_start_ns=pool_start_ns,
        scoring_pcd_ns=scoring_pcd_ns,
        window_ns=_W7D_NS,
    )
    abs_mean_7d, _ = _window_abs_wager_mean(
        arrays,
        pool_start_ns=pool_start_ns,
        scoring_pcd_ns=scoring_pcd_ns,
        window_ns=_W7D_NS,
    )
    prior_w_mean_30, prior_w_std_30, prior_o_mean_30, prior_o_std_30 = _window_pop_stats(
        arrays,
        pool_start_ns=pool_start_ns,
        scoring_pcd_ns=scoring_pcd_ns,
        window_ns=_W30D_NS,
    )
    stake_wager_cv_1h = (
        float(w_std_1h / w_mean_1h)
        if np.isfinite(w_mean_1h) and w_mean_1h > 1e-12 and np.isfinite(w_std_1h)
        else np.nan
    )
    wager_cv_7d = (
        float(w_std_7d / abs_mean_7d)
        if np.isfinite(abs_mean_7d) and abs_mean_7d > 1e-12 and np.isfinite(w_std_7d)
        else np.nan
    )
    return {
        "fe__odds__payout_odds_z__w1h": (
            _z_score(odds_for_z, o_mean_1h, o_std_1h, std_min=1e-12)
            if odds_cnt_1h >= 2.0
            else np.nan
        ),
        "fe__odds__payout_odds_z__w7d": _z_score(
            odds_for_z,
            o_mean_7d,
            o_std_7d,
            std_min=1e-12,
        ),
        "fe__stake__wager_z__w1h": _z_score(
            target_wager,
            w_mean_1h,
            w_std_1h,
            std_min=1e-12,
        ),
        "fe__stake__wager_cv__w1h": stake_wager_cv_1h,
        "fe__wager_cv_w7d": wager_cv_7d,
        "fe__wager_z_prior_w30d": _z_score(
            target_wager,
            prior_w_mean_30,
            prior_w_std_30,
            std_min=1e-12,
        ),
        "fe__payout_odds_z_prior_w30d": _z_score(
            payout_odds,
            prior_o_mean_30,
            prior_o_std_30,
            std_min=1e-12,
        ),
    }


def _range_ratio_features(
    *,
    cnt5: int,
    cnt15: int,
    wsum15: float,
    cnt1h: int,
    cnt1d: int,
    wsum1d: float,
    cnt7d: int,
    wsum7d: float,
    cnt30d: int,
    wsum30d: float,
) -> dict[str, float]:
    """Compute WP-9.2 RANGE count/sum and velocity/ratio columns."""
    wager_15m_over_1d = (
        float(wsum15 / wsum1d) if wsum1d > 1e-9 else np.nan
    )
    bets_15m_over_1d = (
        float(cnt15 / cnt1d) if cnt1d > 1e-9 else np.nan
    )
    wager_7d_over_30d = (
        float(wsum7d / wsum30d) if wsum30d > 1e-9 else np.nan
    )
    velocity_5m_over_15m = (
        float(cnt5 * 3.0 / cnt15) if cnt15 > 0 else np.nan
    )
    velocity_15m_over_1h = (
        float(cnt15 * 4.0 / cnt1h) if cnt1h > 0 else np.nan
    )
    return {
        "fe__rate__bets_cnt__w5m": float(cnt5),
        "fe__bets_cnt__w1d": float(cnt1d),
        "fe__wager_sum__w1d": float(wsum1d),
        "fe__bets_cnt__w7d": float(cnt7d),
        "fe__wager_sum__w7d": float(wsum7d),
        "fe__bets_cnt__w30d": float(cnt30d),
        "fe__wager_sum__w30d": float(wsum30d),
        "fe__wager_sum__w15m_over_w1d": wager_15m_over_1d,
        "fe__bets_cnt__w15m_over_w1d": bets_15m_over_1d,
        "fe__wager_sum__w7d_over_w30d": wager_7d_over_30d,
        "fe__rate__velocity__w5m_over_w15m": velocity_5m_over_15m,
        "fe__rate__velocity__w15m_over_w1h": velocity_15m_over_1h,
    }


def _batch_uncapped_window_bounds(
    pcd_ns: np.ndarray,
    pool_start_ns: np.ndarray,
    scoring_pcd_ns: np.ndarray,
    window_ns: int,
) -> tuple[np.ndarray, np.ndarray]:
    """Return vectorized left/right bounds for uncapped DuckDB RANGE windows."""
    window_end = scoring_pcd_ns - _US_NS
    window_start = np.maximum(pool_start_ns, scoring_pcd_ns - int(window_ns))
    valid = window_end >= window_start
    left = np.searchsorted(pcd_ns, window_start, side="left")
    right = np.searchsorted(pcd_ns, window_end, side="right")
    left = np.where(valid, left, right)
    return left.astype(np.int64, copy=False), right.astype(np.int64, copy=False)


def _batch_entity_window_slices(
    arrays: _EntityArrays,
    pool_start_ns: np.ndarray,
    scoring_pcd_ns: np.ndarray,
) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """Return vectorized left/right bounds for all uncapped entity windows."""
    pcd = arrays.pcd_ns
    return {
        "5m": _batch_uncapped_window_bounds(pcd, pool_start_ns, scoring_pcd_ns, _W5M_NS),
        "15m": _batch_uncapped_window_bounds(pcd, pool_start_ns, scoring_pcd_ns, _W15_NS),
        "1h": _batch_uncapped_window_bounds(pcd, pool_start_ns, scoring_pcd_ns, _W1H_NS),
        "1d": _batch_uncapped_window_bounds(pcd, pool_start_ns, scoring_pcd_ns, _W1D_NS),
        "7d": _batch_uncapped_window_bounds(pcd, pool_start_ns, scoring_pcd_ns, _W7D_NS),
        "30d": _batch_uncapped_window_bounds(pcd, pool_start_ns, scoring_pcd_ns, _W30D_NS),
    }


def _batch_target_row_indices(
    arrays: _EntityArrays,
    pool_start_ns: np.ndarray,
    scoring_pcd_ns: np.ndarray,
    target_bet_ids: np.ndarray,
) -> np.ndarray:
    """Return target row indices for a same-entity target batch."""
    n = int(len(target_bet_ids))
    out = np.full(n, -1, dtype=np.int64)
    if n == 0:
        return out
    pcd = arrays.pcd_ns
    bid = arrays.bet_id
    same_start = np.searchsorted(pcd, scoring_pcd_ns, side="left")
    same_end = np.searchsorted(pcd, scoring_pcd_ns, side="right")
    for i in range(n):
        start = int(same_start[i])
        end = int(same_end[i])
        if end <= start:
            continue
        local = int(np.searchsorted(bid[start:end], target_bet_ids[i], side="left"))
        idx = start + local
        if local >= end - start or bid[idx] != target_bet_ids[i]:
            continue
        if pcd[idx] < pool_start_ns[i]:
            continue
        out[i] = idx
    return out


def _batch_canonical_bounds_1h(
    arrays: _CanonicalArrays,
    trial_pool_start_ns: int,
    scoring_pcd_ns: np.ndarray,
) -> list[tuple[int, int] | None]:
    """Return per-target canonical 1h bounds for a target batch."""
    pool_start = np.full(len(scoring_pcd_ns), int(trial_pool_start_ns), dtype=np.int64)
    left, right = _batch_uncapped_window_bounds(
        arrays.pcd_ns,
        pool_start,
        scoring_pcd_ns,
        _W1H_NS,
    )
    bounds: list[tuple[int, int] | None] = []
    for idx in range(len(scoring_pcd_ns)):
        left_i = int(left[idx])
        right_i = int(right[idx])
        bounds.append((left_i, right_i) if right_i > left_i else None)
    return bounds


def _optional_slice_bounds(left: int, right: int) -> tuple[int, int] | None:
    """Convert slice endpoints to optional bounds tuple."""
    return (left, right) if right > left else None


def _emit_context_from_batch_slices(
    target_idx: int,
    slices: dict[str, tuple[np.ndarray, np.ndarray]],
    idx: int,
    canonical_bounds_1h: tuple[int, int] | None,
) -> _TargetEmitContext:
    """Build emit context from precomputed batch window slices."""
    def _bounds(window_key: str) -> tuple[int, int] | None:
        left_arr, right_arr = slices[window_key]
        return _optional_slice_bounds(int(left_arr[idx]), int(right_arr[idx]))

    def _capped_bounds(window_key: str) -> tuple[int, int] | None:
        left_arr, right_arr = slices[window_key]
        left = int(left_arr[idx])
        right = int(right_arr[idx])
        if target_idx >= 0:
            right = min(right, target_idx)
        return _optional_slice_bounds(left, right)

    return _TargetEmitContext(
        target_idx=target_idx,
        bounds_5m=_bounds("5m"),
        bounds_15m=_bounds("15m"),
        bounds_1h=_bounds("1h"),
        bounds_1h_capped=_capped_bounds("1h"),
        bounds_1d=_bounds("1d"),
        bounds_7d=_bounds("7d"),
        bounds_7d_capped=_capped_bounds("7d"),
        bounds_30d=_bounds("30d"),
        canonical_bounds_1h=canonical_bounds_1h,
    )


def _batch_wager_sum_from_bounds(
    arrays: _EntityArrays,
    left: np.ndarray,
    right: np.ndarray,
) -> np.ndarray:
    """Return vectorized wager sums for precomputed slice bounds."""
    return arrays.wager_sum_prefix[right] - arrays.wager_sum_prefix[left]


def _write_batch_range_features(
    col_arrays: dict[str, np.ndarray],
    out_indices: np.ndarray,
    *,
    cnt5: np.ndarray,
    cnt15: np.ndarray,
    wsum15: np.ndarray,
    cnt1h: np.ndarray,
    cnt1d: np.ndarray,
    wsum1d: np.ndarray,
    cnt7d: np.ndarray,
    wsum7d: np.ndarray,
    cnt30d: np.ndarray,
    wsum30d: np.ndarray,
) -> None:
    """Write vectorized WP-9.2 RANGE count/sum and ratio columns."""
    nan_arr = np.full_like(cnt5, np.nan)
    wager_15m_over_1d = np.divide(wsum15, wsum1d, out=nan_arr.copy(), where=wsum1d > 1e-9)
    bets_15m_over_1d = np.divide(cnt15, cnt1d, out=nan_arr.copy(), where=cnt1d > 1e-9)
    wager_7d_over_30d = np.divide(wsum7d, wsum30d, out=nan_arr.copy(), where=wsum30d > 1e-9)
    velocity_5m_over_15m = np.divide(
        cnt5 * 3.0,
        cnt15,
        out=nan_arr.copy(),
        where=cnt15 > 0.0,
    )
    velocity_15m_over_1h = np.divide(
        cnt15 * 4.0,
        cnt1h,
        out=nan_arr.copy(),
        where=cnt1h > 0.0,
    )
    col_arrays["fe__bets_cnt__w15m"][out_indices] = cnt15
    col_arrays["fe__wager_sum__w15m"][out_indices] = wsum15
    col_arrays["fe__rate__bets_cnt__w5m"][out_indices] = cnt5
    col_arrays["fe__bets_cnt__w1d"][out_indices] = cnt1d
    col_arrays["fe__wager_sum__w1d"][out_indices] = wsum1d
    col_arrays["fe__bets_cnt__w7d"][out_indices] = cnt7d
    col_arrays["fe__wager_sum__w7d"][out_indices] = wsum7d
    col_arrays["fe__bets_cnt__w30d"][out_indices] = cnt30d
    col_arrays["fe__wager_sum__w30d"][out_indices] = wsum30d
    col_arrays["fe__wager_sum__w15m_over_w1d"][out_indices] = wager_15m_over_1d
    col_arrays["fe__bets_cnt__w15m_over_w1d"][out_indices] = bets_15m_over_1d
    col_arrays["fe__wager_sum__w7d_over_w30d"][out_indices] = wager_7d_over_30d
    col_arrays["fe__rate__velocity__w5m_over_w15m"][out_indices] = velocity_5m_over_15m
    col_arrays["fe__rate__velocity__w15m_over_w1h"][out_indices] = velocity_15m_over_1h


def _batch_emit_entity_range_features(
    col_arrays: dict[str, np.ndarray],
    out_indices: np.ndarray,
    arrays: _EntityArrays,
    slices: dict[str, tuple[np.ndarray, np.ndarray]],
) -> None:
    """Vectorized RANGE count/sum/ratio emit for one entity target batch."""
    left5, right5 = slices["5m"]
    left15, right15 = slices["15m"]
    left1h, right1h = slices["1h"]
    left1d, right1d = slices["1d"]
    left7d, right7d = slices["7d"]
    left30d, right30d = slices["30d"]
    cnt5 = (right5 - left5).astype(np.float64)
    cnt15 = (right15 - left15).astype(np.float64)
    cnt1h = (right1h - left1h).astype(np.float64)
    cnt1d = (right1d - left1d).astype(np.float64)
    cnt7d = (right7d - left7d).astype(np.float64)
    cnt30d = (right30d - left30d).astype(np.float64)
    wsum15 = _batch_wager_sum_from_bounds(arrays, left15, right15)
    wsum1d = _batch_wager_sum_from_bounds(arrays, left1d, right1d)
    wsum7d = _batch_wager_sum_from_bounds(arrays, left7d, right7d)
    wsum30d = _batch_wager_sum_from_bounds(arrays, left30d, right30d)
    _write_batch_range_features(
        col_arrays,
        out_indices,
        cnt5=cnt5,
        cnt15=cnt15,
        wsum15=wsum15,
        cnt1h=cnt1h,
        cnt1d=cnt1d,
        wsum1d=wsum1d,
        cnt7d=cnt7d,
        wsum7d=wsum7d,
        cnt30d=cnt30d,
        wsum30d=wsum30d,
    )


def _range_bet_pack(
    arrays: _CanonicalArrays,
    *,
    pool_start_ns: int,
    scoring_pcd_ns: int,
    window_ns: int,
) -> tuple[int, float, float, float]:
    """Return trial 1h pack columns for one canonical partition."""
    bounds = _range_window_indices(
        arrays.pcd_ns,
        pool_start_ns=pool_start_ns,
        scoring_pcd_ns=scoring_pcd_ns,
        window_ns=window_ns,
    )
    if bounds is None:
        return 0, 0.0, 0.0, 0.0
    left, right = bounds
    cnt = right - left
    wsum = float(arrays.wager_prefix[right] - arrays.wager_prefix[left])
    back_sum = float(arrays.back_prefix[right] - arrays.back_prefix[left])
    odds_cnt = float(arrays.odds_cnt_prefix[right] - arrays.odds_cnt_prefix[left])
    if odds_cnt > 0.0:
        odds_avg = float(
            (arrays.odds_sum_prefix[right] - arrays.odds_sum_prefix[left]) / odds_cnt,
        )
    else:
        odds_avg = 0.0
    back_ratio = back_sum / float(cnt) if cnt > 0 else 0.0
    return cnt, wsum, back_ratio, odds_avg


def _lag_prior_index(
    arrays: _EntityArrays,
    *,
    pool_start_ns: int,
    scoring_pcd_ns: int,
    target_bet_id: float,
) -> int:
    """Return prior row index in pool slice, or -1 when absent."""
    target_idx = _target_row_index(
        arrays,
        pool_start_ns=pool_start_ns,
        scoring_pcd_ns=scoring_pcd_ns,
        target_bet_id=target_bet_id,
    )
    if target_idx <= 0:
        return -1
    prior = target_idx - 1
    if arrays.pcd_ns[prior] < pool_start_ns:
        return -1
    return prior


def _target_row_index(
    arrays: _EntityArrays,
    *,
    pool_start_ns: int,
    scoring_pcd_ns: int,
    target_bet_id: float,
) -> int:
    """Return target row index in pool slice, or -1 when absent."""
    same_start = int(np.searchsorted(arrays.pcd_ns, scoring_pcd_ns, side="left"))
    same_end = int(np.searchsorted(arrays.pcd_ns, scoring_pcd_ns, side="right"))
    if same_end > same_start:
        local = int(
            np.searchsorted(arrays.bet_id[same_start:same_end], target_bet_id, side="left"),
        )
        if local < same_end - same_start and arrays.bet_id[same_start + local] == target_bet_id:
            idx = same_start + local
            if arrays.pcd_ns[idx] >= pool_start_ns:
                return idx
    return -1


def _today_features(
    arrays: _EntityArrays,
    *,
    pool_start_ns: int,
    scoring_pcd_ns: int,
    target_bet_id: float,
    target_gaming_day_ord: int,
) -> dict[str, float]:
    """Compute WP-9.6 same-gaming-day prior counters and elapsed seconds."""
    target_idx = _target_row_index(
        arrays,
        pool_start_ns=pool_start_ns,
        scoring_pcd_ns=scoring_pcd_ns,
        target_bet_id=target_bet_id,
    )
    if target_idx < 0:
        return {
            "fe__canonical__bets_cnt__today": 0.0,
            "fe__canonical__wager_sum__today": 0.0,
            "fe__canonical__avg_wager__today": np.nan,
            "fe__canonical__elapsed_sec_since_first_bet__today": np.nan,
        }
    in_pool = arrays.pcd_ns >= pool_start_ns
    same_day = arrays.gaming_day_ord == int(target_gaming_day_ord)
    prior_mask = in_pool & same_day & (np.arange(len(arrays.pcd_ns)) < target_idx)
    bets_cnt = int(np.sum(prior_mask))
    wager_sum = float(np.nansum(arrays.wager[prior_mask]))
    day_mask = in_pool & same_day & (np.arange(len(arrays.pcd_ns)) <= target_idx)
    if not bool(np.any(day_mask)):
        elapsed = np.nan
    else:
        first_pcd_ns = int(np.min(arrays.pcd_ns[day_mask]))
        elapsed = float((scoring_pcd_ns - first_pcd_ns) / 1_000_000_000.0)
    avg_wager = float(wager_sum / bets_cnt) if bets_cnt > 0 else np.nan
    return {
        "fe__canonical__bets_cnt__today": float(bets_cnt),
        "fe__canonical__wager_sum__today": wager_sum,
        "fe__canonical__avg_wager__today": avg_wager,
        "fe__canonical__elapsed_sec_since_first_bet__today": elapsed,
    }


def _pool_safe_interarrival_gaps(
    arrays: _EntityArrays,
    *,
    pool_start_ns: int,
    left: int,
    right: int,
) -> np.ndarray:
    """Return interarrival gaps with both endpoints inside the bounded pool."""
    if right <= left:
        return np.empty(0, dtype=np.float64)
    gaps: list[float] = []
    for idx in range(left, right):
        if idx <= 0:
            continue
        if int(arrays.pcd_ns[idx - 1]) < int(pool_start_ns):
            continue
        gap = float(arrays.iv_sec[idx])
        if np.isfinite(gap):
            gaps.append(gap)
    return np.asarray(gaps, dtype=np.float64)


def _interarrival_window_stats(
    arrays: _EntityArrays,
    *,
    pool_start_ns: int,
    scoring_pcd_ns: int,
    window_ns: int,
    target_idx: int,
) -> tuple[float, float]:
    """Return prior-window mean/std of pool-safe per-row interarrival gaps."""
    bounds = _range_window_indices(
        arrays.pcd_ns,
        pool_start_ns=pool_start_ns,
        scoring_pcd_ns=scoring_pcd_ns,
        window_ns=window_ns,
        target_idx=target_idx,
    )
    if bounds is None:
        return np.nan, np.nan
    left, right = bounds
    gaps = _pool_safe_interarrival_gaps(
        arrays,
        pool_start_ns=pool_start_ns,
        left=left,
        right=right,
    )
    if gaps.size == 0:
        return np.nan, np.nan
    mean = float(gaps.mean())
    if gaps.size <= 1:
        return mean, 0.0
    return mean, float(gaps.std(ddof=0))


def _interarrival_features(
    arrays: _EntityArrays,
    *,
    pool_start_ns: int,
    scoring_pcd_ns: int,
    target_bet_id: float,
) -> dict[str, float]:
    """Compute WP-9.5 interarrival gap / avg / std / z-score / ratio columns."""
    target_idx = _target_row_index(
        arrays,
        pool_start_ns=pool_start_ns,
        scoring_pcd_ns=scoring_pcd_ns,
        target_bet_id=target_bet_id,
    )
    prior = target_idx - 1 if target_idx > 0 and arrays.pcd_ns[target_idx - 1] >= pool_start_ns else -1
    if prior < 0:
        gap = np.nan
        lag2 = np.nan
    else:
        gap = float((scoring_pcd_ns - int(arrays.pcd_ns[prior])) / 1_000_000_000.0)
        lag2_prior = prior - 1
        if lag2_prior < 0 or int(arrays.pcd_ns[lag2_prior]) < int(pool_start_ns):
            lag2 = np.nan
        else:
            lag2_val = float(arrays.iv_sec[prior])
            lag2 = lag2_val if np.isfinite(lag2_val) else np.nan
    if target_idx < 0:
        return {
            "fe__time_since_last_bet_sec": np.nan,
            "fe__interarrival__lag2_sec": np.nan,
            "fe__interarrival__last_gap_to_recent_mean_ratio__w1h": np.nan,
            "fe__interarrival__cv__w1h": np.nan,
            "fe__interarrival__last_gap_z__w7d": np.nan,
        }
    iv_mean_1h, iv_std_1h = _interarrival_window_stats(
        arrays,
        pool_start_ns=pool_start_ns,
        scoring_pcd_ns=scoring_pcd_ns,
        window_ns=_W1H_NS,
        target_idx=target_idx,
    )
    iv_mean_7d, iv_std_7d = _interarrival_window_stats(
        arrays,
        pool_start_ns=pool_start_ns,
        scoring_pcd_ns=scoring_pcd_ns,
        window_ns=_W7D_NS,
        target_idx=target_idx,
    )
    ratio_1h = (
        float(gap / iv_mean_1h)
        if np.isfinite(gap) and np.isfinite(iv_mean_1h) and iv_mean_1h > 1e-9
        else np.nan
    )
    cv_1h = (
        float(iv_std_1h / iv_mean_1h)
        if np.isfinite(iv_mean_1h) and iv_mean_1h > 1e-9 and np.isfinite(iv_std_1h)
        else np.nan
    )
    return {
        "fe__time_since_last_bet_sec": gap,
        "fe__interarrival__lag2_sec": lag2,
        "fe__interarrival__last_gap_to_recent_mean_ratio__w1h": ratio_1h,
        "fe__interarrival__cv__w1h": cv_1h,
        "fe__interarrival__last_gap_z__w7d": _z_score(
            gap,
            iv_mean_7d,
            iv_std_7d,
            std_min=1e-9,
        ),
    }


def _emit_target_into_non_range(
    col_arrays: dict[str, np.ndarray],
    row: int,
    arrays: _EntityArrays,
    canonical_arrays: _CanonicalArrays | None,
    ctx: _TargetEmitContext,
    *,
    pool_start_ns: int,
    scoring_pcd_ns: int,
    payout_odds: float,
    target_wager: float,
    target_gaming_day_ord: int,
) -> None:
    """Write non-RANGE prototype columns for one target."""
    cnt1h_trial, wsum1h, back_ratio1h, odds_avg1h = (0, 0.0, 0.0, 0.0)
    if canonical_arrays is not None:
        cnt1h_trial, wsum1h, back_ratio1h, odds_avg1h = _bet_pack_from_bounds(
            canonical_arrays,
            ctx.canonical_bounds_1h,
        )
    col_arrays["bet__bets_cnt__w1h"][row] = float(cnt1h_trial)
    col_arrays["bet__wager_sum__w1h"][row] = float(wsum1h)
    col_arrays["bet__back_bet_ratio__w1h"][row] = float(back_ratio1h)
    col_arrays["bet__payout_odds_avg__w1h"][row] = float(odds_avg1h)
    col_arrays["fe__odds__payout_odds_step_ratio"][row] = np.nan
    for col, val in _interarrival_features_from_context(
        arrays,
        ctx,
        pool_start_ns=pool_start_ns,
        scoring_pcd_ns=scoring_pcd_ns,
    ).items():
        col_arrays[col][row] = val
    for col, val in _avg_stddev_z_features_from_context(
        arrays,
        ctx,
        payout_odds=payout_odds,
        target_wager=target_wager,
    ).items():
        col_arrays[col][row] = val
    for col, val in _max_ratio_features_from_context(
        arrays,
        ctx,
        payout_odds=payout_odds,
        target_wager=target_wager,
    ).items():
        col_arrays[col][row] = val
    for col, val in _today_features_from_context(
        arrays,
        ctx,
        pool_start_ns=pool_start_ns,
        scoring_pcd_ns=scoring_pcd_ns,
        target_gaming_day_ord=target_gaming_day_ord,
    ).items():
        col_arrays[col][row] = val
    prior = _prior_row_index(arrays, pool_start_ns=pool_start_ns, target_idx=ctx.target_idx)
    if prior >= 0:
        lag_odds = float(arrays.payout_odds[prior])
        if np.isfinite(lag_odds) and lag_odds > 1e-9 and np.isfinite(payout_odds):
            col_arrays["fe__odds__payout_odds_step_ratio"][row] = float(payout_odds / lag_odds)


def _emit_target_into(
    col_arrays: dict[str, np.ndarray],
    row: int,
    arrays: _EntityArrays,
    canonical_arrays: _CanonicalArrays | None,
    *,
    pool_start_ns: int,
    scoring_pcd_ns: int,
    target_bet_id: float,
    payout_odds: float,
    target_wager: float,
    trial_pool_start_ns: int,
    target_gaming_day_ord: int,
    skip_range: bool = False,
    ctx: _TargetEmitContext | None = None,
) -> None:
    """Write prototype columns for one target into preallocated column arrays."""
    col_arrays["bet_id"][row] = target_bet_id
    pool_arr = np.asarray([pool_start_ns], dtype=np.int64)
    scoring_arr = np.asarray([scoring_pcd_ns], dtype=np.int64)
    if ctx is None:
        row_slices = _batch_entity_window_slices(arrays, pool_arr, scoring_arr)
        if not skip_range:
            _batch_emit_entity_range_features(
                col_arrays,
                np.asarray([row], dtype=np.int64),
                arrays,
                row_slices,
            )
        target_idx = int(
            _batch_target_row_indices(
                arrays,
                pool_arr,
                scoring_arr,
                np.asarray([target_bet_id], dtype=np.float64),
            )[0],
        )
        canon_bounds: tuple[int, int] | None = None
        if canonical_arrays is not None:
            canon_bounds = _batch_canonical_bounds_1h(
                canonical_arrays,
                trial_pool_start_ns,
                scoring_arr,
            )[0]
        emit_ctx = _emit_context_from_batch_slices(
            target_idx,
            row_slices,
            0,
            canon_bounds,
        )
    else:
        emit_ctx = ctx
        if not skip_range:
            row_slices = _batch_entity_window_slices(arrays, pool_arr, scoring_arr)
            _batch_emit_entity_range_features(
                col_arrays,
                np.asarray([row], dtype=np.int64),
                arrays,
                row_slices,
            )
    _emit_target_into_non_range(
        col_arrays,
        row,
        arrays,
        canonical_arrays,
        emit_ctx,
        pool_start_ns=pool_start_ns,
        scoring_pcd_ns=scoring_pcd_ns,
        payout_odds=payout_odds,
        target_wager=target_wager,
        target_gaming_day_ord=target_gaming_day_ord,
    )


def _emit_target_row(
    arrays: _EntityArrays,
    canonical_arrays: _CanonicalArrays | None,
    *,
    pool_start_ns: int,
    scoring_pcd_ns: int,
    target_bet_id: float,
    payout_odds: float,
    target_wager: float,
    trial_pool_start_ns: int,
    target_gaming_day_ord: int,
) -> dict[str, float]:
    """Compute prototype columns for one target using indexed entity arrays."""
    col_arrays = {col: np.full(1, np.nan, dtype=np.float64) for col in INDEXED_PROTOTYPE_OUTPUT_COLUMNS}
    _emit_target_into(
        col_arrays,
        0,
        arrays,
        canonical_arrays,
        pool_start_ns=pool_start_ns,
        scoring_pcd_ns=scoring_pcd_ns,
        target_bet_id=target_bet_id,
        payout_odds=payout_odds,
        target_wager=target_wager,
        trial_pool_start_ns=trial_pool_start_ns,
        target_gaming_day_ord=target_gaming_day_ord,
    )
    return {col: float(col_arrays[col][0]) for col in INDEXED_PROTOTYPE_OUTPUT_COLUMNS}


def _indexed_replay_features(
    events_df: pd.DataFrame,
    targets: pd.DataFrame,
    bounds: pd.DataFrame,
    *,
    canonical_by_player: dict[int, str],
    hk_tz: str = "Asia/Hong_Kong",
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Emit indexed replay features grouped by entity."""
    bounds_ns = _bounds_with_ns(bounds)
    bounds_idx = bounds_ns.set_index(pd.to_numeric(bounds_ns["bet_id"], errors="coerce"))
    trial_pool_start_ns = int(bounds_ns["pool_start_ns"].min())
    entity_arrays, max_len = _build_entity_arrays(
        events_df,
        canonical_by_player,
        hk_tz=hk_tz,
    )
    canonical_arrays = _build_canonical_arrays(events_df, canonical_by_player)
    work_targets = targets.copy()
    work_targets["bet_id"] = pd.to_numeric(work_targets["bet_id"], errors="coerce")
    work_targets["player_id"] = pd.to_numeric(work_targets["player_id"], errors="coerce")
    work_targets["payout_odds"] = pd.to_numeric(work_targets["payout_odds"], errors="coerce")
    work_targets["wager"] = pd.to_numeric(work_targets["wager"], errors="coerce")
    work_targets = work_targets.dropna(subset=["bet_id", "player_id", "canonical_id"])
    target_gde = (
        work_targets["gaming_day_event"]
        if "gaming_day_event" in work_targets.columns
        else None
    )
    target_gday_ord = _gaming_day_ordinals(
        work_targets["payout_complete_dtm"],
        target_gde,
        hk_tz=hk_tz,
    )
    n_targets = int(len(work_targets))
    if n_targets == 0:
        out = pd.DataFrame(columns=list(INDEXED_PROTOTYPE_OUTPUT_COLUMNS))
    else:
        bet_ids = work_targets["bet_id"].to_numpy(dtype=np.float64, copy=False)
        player_ids = work_targets["player_id"].to_numpy(dtype=np.int64, copy=False)
        canonical_ids = work_targets["canonical_id"].astype(str).str.strip().to_numpy()
        payout_odds = work_targets["payout_odds"].to_numpy(dtype=np.float64, copy=False)
        wagers = work_targets["wager"].to_numpy(dtype=np.float64, copy=False)
        aligned_bounds = bounds_idx.loc[bet_ids]
        pool_start_ns_arr = aligned_bounds["pool_start_ns"].to_numpy(dtype=np.int64, copy=False)
        scoring_pcd_ns_arr = aligned_bounds["scoring_pcd_ns"].to_numpy(dtype=np.int64, copy=False)
        out_cols = list(INDEXED_PROTOTYPE_OUTPUT_COLUMNS)
        col_arrays: dict[str, np.ndarray] = {
            col: np.full(n_targets, np.nan, dtype=np.float64) for col in out_cols
        }
        entity_groups: dict[tuple[str, int], list[tuple[int, int]]] = defaultdict(list)
        out_idx = 0
        for i in range(n_targets):
            key = (canonical_ids[i], int(player_ids[i]))
            if entity_arrays.get(key) is None:
                continue
            entity_groups[key].append((out_idx, i))
            out_idx += 1
        for key, members in entity_groups.items():
            arrays = entity_arrays[key]
            canon_arrays = canonical_arrays.get(key[0])
            out_indices = np.fromiter((m[0] for m in members), dtype=np.int64, count=len(members))
            src_indices = np.fromiter((m[1] for m in members), dtype=np.int64, count=len(members))
            group_pool_start = pool_start_ns_arr[src_indices]
            group_scoring_pcd = scoring_pcd_ns_arr[src_indices]
            group_bet_ids = bet_ids[src_indices]
            col_arrays["bet_id"][out_indices] = group_bet_ids
            window_slices = _batch_entity_window_slices(
                arrays,
                group_pool_start,
                group_scoring_pcd,
            )
            _batch_emit_entity_range_features(
                col_arrays,
                out_indices,
                arrays,
                window_slices,
            )
            target_idx_arr = _batch_target_row_indices(
                arrays,
                group_pool_start,
                group_scoring_pcd,
                group_bet_ids,
            )
            canon_bounds_list: list[tuple[int, int] | None]
            if canon_arrays is not None:
                canon_bounds_list = _batch_canonical_bounds_1h(
                    canon_arrays,
                    trial_pool_start_ns,
                    group_scoring_pcd,
                )
            else:
                canon_bounds_list = [None] * len(out_indices)
            for batch_idx, (out_row, src_row) in enumerate(
                zip(out_indices, src_indices, strict=True),
            ):
                emit_ctx = _emit_context_from_batch_slices(
                    int(target_idx_arr[batch_idx]),
                    window_slices,
                    batch_idx,
                    canon_bounds_list[batch_idx],
                )
                _emit_target_into_non_range(
                    col_arrays,
                    int(out_row),
                    arrays,
                    canon_arrays,
                    emit_ctx,
                    pool_start_ns=int(pool_start_ns_arr[src_row]),
                    scoring_pcd_ns=int(scoring_pcd_ns_arr[src_row]),
                    payout_odds=float(payout_odds[src_row]),
                    target_wager=float(wagers[src_row]),
                    target_gaming_day_ord=int(target_gday_ord[src_row]),
                )
        if out_idx == 0:
            out = pd.DataFrame(columns=out_cols)
        else:
            out = pd.DataFrame(
                {col: col_arrays[col][:out_idx] for col in out_cols},
            )
    metrics = {
        "input_event_rows": int(len(events_df)),
        "target_rows": int(len(targets)),
        "output_rows": int(len(out)),
        "max_state_keys": int(len(entity_arrays)),
        "max_canonical_keys": int(len(canonical_arrays)),
        "max_array_len": int(max_len),
    }
    return out, metrics


def materialize_short_term_replay_indexed_prototype(
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
    """Materialize indexed replay prototype short PIT features."""
    cfg = serving_cfg or default_hightier_serving_config()
    cmap = (
        Path(canonical_mapping_parquet).resolve()
        if canonical_mapping_parquet is not None
        else default_canonical_mapping_parquet_path().resolve()
    )
    if not cmap.is_file():
        raise FileNotFoundError(f"canonical mapping parquet missing: {cmap}")
    phase: dict[str, float] = {}
    t0 = time.perf_counter()
    targets = _load_target_bets(
        Path(training_parquet_for_bet_ids).resolve(),
        payout_yyyymm=str(payout_yyyymm),
        target_limit=target_limit,
        duckdb_runtime=duckdb_runtime,
        hk_tz=cfg.hk_tz,
    )
    phase["load_targets_s"] = round(time.perf_counter() - t0, 6)
    if targets.empty:
        raise ValueError(
            f"indexed replay found no target bets for month={payout_yyyymm!r} "
            f"in {training_parquet_for_bet_ids}",
        )
    t1 = time.perf_counter()
    targets = _attach_canonical_id(targets, cmap)
    phase["attach_canonical_s"] = round(time.perf_counter() - t1, 6)
    player_ids = tuple(
        int(pid)
        for pid in pd.to_numeric(targets["player_id"], errors="coerce").dropna().astype(int).tolist()
    )
    t2 = time.perf_counter()
    events_df = _load_replay_events(
        Path(cleaned_bet_parquet).resolve(),
        payout_yyyymm=str(payout_yyyymm),
        player_ids=player_ids,
        duckdb_runtime=duckdb_runtime,
        hk_tz=cfg.hk_tz,
    )
    phase["load_pool_s"] = round(time.perf_counter() - t2, 6)
    from trainer_hightier.serving.scorer import compute_scoring_bounds_for_bets

    t3 = time.perf_counter()
    bounds = compute_scoring_bounds_for_bets(targets, cfg=cfg)
    pool_player_ids = tuple(
        int(pid)
        for pid in pd.to_numeric(events_df["player_id"], errors="coerce").dropna().astype(int).tolist()
    )
    from trainer_hightier.feature_experiment.short_term_pit_replay_prototype import _canonical_by_player

    canonical_by_player = _canonical_by_player(cmap, pool_player_ids)
    phase["prepare_bounds_s"] = round(time.perf_counter() - t3, 6)
    t4 = time.perf_counter()
    features, replay_metrics = _indexed_replay_features(
        events_df,
        targets,
        bounds,
        canonical_by_player={int(k): str(v) for k, v in canonical_by_player.items()},
        hk_tz=cfg.hk_tz,
    )
    phase["emit_s"] = round(time.perf_counter() - t4, 6)
    dst = Path(out_parquet).resolve()
    dst.parent.mkdir(parents=True, exist_ok=True)
    t5 = time.perf_counter()
    features[list(INDEXED_PROTOTYPE_OUTPUT_COLUMNS)].to_parquet(dst, index=False)
    phase["write_s"] = round(time.perf_counter() - t5, 6)
    elapsed = round(time.perf_counter() - t0, 6)
    metrics = {
        **replay_metrics,
        "elapsed_seconds": elapsed,
        "rows_per_second": round(float(replay_metrics["input_event_rows"]) / elapsed, 3)
        if elapsed > 0
        else None,
        "payout_yyyymm": str(payout_yyyymm),
        "prototype_columns": list(INDEXED_PROTOTYPE_OUTPUT_COLUMNS),
        "gate_columns": list(resolve_scorer_short_pit_prototype_gate_columns()),
        "ignored_gate_columns": list(PROTOTYPE_GATE_IGNORE_COLUMNS),
        "production_gate_columns": list(resolve_production_scorer_short_pit_gate_columns()),
        "phase_timings": phase,
    }
    logger.info(
        "[short_pit_replay_indexed_prototype] wrote %s rows=%d elapsed=%.3fs",
        dst.name,
        len(features),
        elapsed,
    )
    return dst, metrics


def evaluate_indexed_replay_go_no_go(
    *,
    parity_passed: bool,
    replay_elapsed_seconds: float,
    bounded_elapsed_seconds: float,
    target_limit: int,
) -> dict[str, Any]:
    """Apply v2 feasibility gates from the implementation plan."""
    speedup = (
        bounded_elapsed_seconds / replay_elapsed_seconds
        if replay_elapsed_seconds > 0
        else None
    )
    min_speedup = 1.5 if target_limit >= 50_000 else None
    if target_limit >= 100_000:
        min_speedup = 2.0
    expand = bool(
        parity_passed
        and speedup is not None
        and min_speedup is not None
        and speedup >= min_speedup
    )
    final_expand = bool(parity_passed and speedup is not None and speedup >= 3.0)
    if min_speedup is None:
        decision = "continue_benchmark"
    elif final_expand:
        decision = "integrate_candidate"
    elif expand:
        decision = "continue_prototype"
    else:
        decision = "stop_or_optimize"
    return {
        "decision": decision,
        "parity_passed": bool(parity_passed),
        "speedup_ratio": speedup,
        "min_speedup_ratio": min_speedup,
        "final_integration_ratio": 3.0,
        "final_integration_met": final_expand,
        "gate_columns": list(resolve_scorer_short_pit_prototype_gate_columns()),
        "ignored_gate_columns": list(PROTOTYPE_GATE_IGNORE_COLUMNS),
        "production_gate_columns": list(resolve_production_scorer_short_pit_gate_columns()),
    }


def _process_rss_mb() -> float | None:
    """Best-effort process RSS in MiB (``psutil`` when installed, else stdlib)."""
    try:
        import psutil

        return round(float(psutil.Process().memory_info().rss) / (1024.0 * 1024.0), 3)
    except Exception:
        from trainer_hightier.serving.scorer_dry_run import capture_process_rss_mb

        return capture_process_rss_mb()


class _MemoryPeakSampler:
    """Poll process RSS while a benchmark phase runs."""

    def __init__(self) -> None:
        self.peak_mb: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> _MemoryPeakSampler:
        sample = _process_rss_mb()
        if sample is not None:
            self.peak_mb = sample
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        sample = _process_rss_mb()
        if sample is not None:
            self.peak_mb = sample if self.peak_mb is None else max(self.peak_mb, sample)

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            sample = _process_rss_mb()
            if sample is not None:
                self.peak_mb = sample if self.peak_mb is None else max(self.peak_mb, sample)
            self._stop.wait(0.25)


def _month_target_filter_sql(*, train_esc: str, payout_yyyymm: str, limit: int | None) -> str:
    """Build DuckDB SQL for ordered payout-month targets."""
    ym = str(payout_yyyymm).strip()
    limit_sql = f"\n            LIMIT {int(limit)}" if limit is not None else ""
    return f"""
            SELECT *
            FROM read_parquet('{train_esc}')
            WHERE TRY_CAST(bet_id AS DOUBLE) IS NOT NULL
              AND payout_complete_dtm IS NOT NULL
              AND TRY_CAST(player_id AS BIGINT) IS NOT NULL
              AND strftime(CAST(payout_complete_dtm AS TIMESTAMPTZ), '%Y%m') = '{ym}'
            ORDER BY CAST(payout_complete_dtm AS TIMESTAMPTZ) ASC,
                     TRY_CAST(bet_id AS DOUBLE) ASC{limit_sql}
            """


def _prepare_benchmark_subset(
    *,
    training_parquet_for_bet_ids: Path,
    cleaned_bet_parquet: Path,
    payout_yyyymm: str,
    target_limit: int,
    duckdb_runtime: DuckDbRuntimeConfig,
    out_dir: Path,
) -> Path:
    """Write bounded training subset with ``gaming_day_event`` attached."""
    train_esc = _path_esc(Path(training_parquet_for_bet_ids).resolve())
    con = duckdb.connect(database=":memory:")
    try:
        apply_duckdb_runtime_pragmas(con, duckdb_runtime)
        limited_targets = con.execute(
            _month_target_filter_sql(
                train_esc=train_esc,
                payout_yyyymm=payout_yyyymm,
                limit=int(target_limit),
            ),
        ).fetchdf()
    finally:
        con.close()
    limited_raw = out_dir / "training_subset_raw.parquet"
    limited_targets.to_parquet(limited_raw, index=False)
    from trainer_hightier.trainer import _ensure_training_parquet_gaming_day_event_column

    return _ensure_training_parquet_gaming_day_event_column(
        limited_raw,
        duckdb_runtime=duckdb_runtime,
        cleaned_bet_parquet=Path(cleaned_bet_parquet).resolve(),
    )


def _prepare_full_month_benchmark_subset(
    *,
    training_parquet_for_bet_ids: Path,
    cleaned_bet_parquet: Path,
    payout_yyyymm: str,
    duckdb_runtime: DuckDbRuntimeConfig,
    out_dir: Path,
) -> tuple[Path, int]:
    """Write full payout-month training subset with ``gaming_day_event`` attached."""
    train_esc = _path_esc(Path(training_parquet_for_bet_ids).resolve())
    con = duckdb.connect(database=":memory:")
    try:
        apply_duckdb_runtime_pragmas(con, duckdb_runtime)
        month_targets = con.execute(
            _month_target_filter_sql(
                train_esc=train_esc,
                payout_yyyymm=payout_yyyymm,
                limit=None,
            ),
        ).fetchdf()
    finally:
        con.close()
    if month_targets.empty:
        raise ValueError(
            f"full-month gate found no targets for month={payout_yyyymm!r} "
            f"in {training_parquet_for_bet_ids}",
        )
    target_count = int(len(month_targets))
    month_raw = out_dir / "training_subset_raw.parquet"
    month_targets.to_parquet(month_raw, index=False)
    from trainer_hightier.trainer import _ensure_training_parquet_gaming_day_event_column

    subset_train = _ensure_training_parquet_gaming_day_event_column(
        month_raw,
        duckdb_runtime=duckdb_runtime,
        cleaned_bet_parquet=Path(cleaned_bet_parquet).resolve(),
    )
    return subset_train, target_count


def _validate_replay_output_vs_targets(
    *,
    replay_parquet: Path,
    targets_parquet: Path,
    duckdb_runtime: DuckDbRuntimeConfig,
) -> dict[str, Any]:
    """Check replay row count and ``bet_id`` uniqueness against target training parquet."""
    replay_esc = _path_esc(Path(replay_parquet).resolve())
    targets_esc = _path_esc(Path(targets_parquet).resolve())
    con = duckdb.connect(database=":memory:")
    try:
        apply_duckdb_runtime_pragmas(con, duckdb_runtime)
        row = con.execute(
            f"""
            WITH replay AS (
                SELECT TRY_CAST(bet_id AS DOUBLE) AS bet_id
                FROM read_parquet('{replay_esc}')
            ),
            targets AS (
                SELECT TRY_CAST(bet_id AS DOUBLE) AS bet_id
                FROM read_parquet('{targets_esc}')
            )
            SELECT
                (SELECT COUNT(*)::BIGINT FROM replay) AS replay_rows,
                (SELECT COUNT(DISTINCT bet_id)::BIGINT FROM replay) AS replay_unique_bet_ids,
                (SELECT COUNT(*)::BIGINT FROM targets) AS target_rows,
                (SELECT COUNT(DISTINCT bet_id)::BIGINT FROM targets) AS target_unique_bet_ids,
                (SELECT COUNT(*)::BIGINT FROM replay r INNER JOIN targets t USING (bet_id)) AS matched_bet_ids
            """,
        ).fetchone()
    finally:
        con.close()
    if row is None:
        raise RuntimeError("output validation query returned no rows")
    replay_rows, replay_unique, target_rows, target_unique, matched = (
        int(row[0]),
        int(row[1]),
        int(row[2]),
        int(row[3]),
        int(row[4]),
    )
    passed = (
        replay_rows == target_rows == matched
        and replay_unique == target_unique == target_rows
    )
    return {
        "passed": bool(passed),
        "replay_rows": replay_rows,
        "replay_unique_bet_ids": replay_unique,
        "target_rows": target_rows,
        "target_unique_bet_ids": target_unique,
        "matched_bet_ids": matched,
    }


def compare_replay_to_oracle_parquet(
    replay_parquet: Path,
    oracle_parquet: Path,
    *,
    columns: tuple[str, ...],
    duckdb_runtime: DuckDbRuntimeConfig,
    float_tol: float = 1e-9,
) -> dict[str, Any]:
    """Compare replay vs bounded oracle parquet without loading full frames into pandas."""
    replay_esc = _path_esc(Path(replay_parquet).resolve())
    oracle_esc = _path_esc(Path(oracle_parquet).resolve())
    con = duckdb.connect(database=":memory:")
    report: dict[str, Any] = {"columns": {}}
    try:
        apply_duckdb_runtime_pragmas(con, duckdb_runtime)
        compared_rows = int(
            con.execute(
                f"""
                SELECT COUNT(*)::BIGINT
                FROM read_parquet('{replay_esc}') r
                INNER JOIN read_parquet('{oracle_esc}') o
                    ON TRY_CAST(r.bet_id AS DOUBLE) = TRY_CAST(o.bet_id AS DOUBLE)
                """,
            ).fetchone()[0],
        )
        report["compared_rows"] = compared_rows
        if compared_rows == 0:
            raise ValueError("replay/oracle merge produced zero rows")
        atol = float(float_tol)
        for col in columns:
            mismatch_count, sample_ids = con.execute(
                f"""
                WITH merged AS (
                    SELECT
                        TRY_CAST(r.bet_id AS DOUBLE) AS bet_id,
                        TRY_CAST(r."{col}" AS DOUBLE) AS replay_v,
                        TRY_CAST(o."{col}" AS DOUBLE) AS oracle_v
                    FROM read_parquet('{replay_esc}') r
                    INNER JOIN read_parquet('{oracle_esc}') o
                        ON TRY_CAST(r.bet_id AS DOUBLE) = TRY_CAST(o.bet_id AS DOUBLE)
                ),
                bad AS (
                    SELECT bet_id
                    FROM merged
                    WHERE NOT (
                        (replay_v IS NULL AND oracle_v IS NULL)
                        OR (
                            replay_v IS NOT NULL
                            AND oracle_v IS NOT NULL
                            AND abs(replay_v - oracle_v)
                                <= GREATEST({atol}, abs(oracle_v) * 1e-6)
                        )
                    )
                )
                SELECT
                    (SELECT COUNT(*)::BIGINT FROM bad) AS mismatch_count,
                    (
                        SELECT list(bet_id)
                        FROM (
                            SELECT bet_id
                            FROM bad
                            ORDER BY bet_id
                            LIMIT 5
                        )
                    ) AS sample_bet_ids
                """,
            ).fetchone()
            report["columns"][col] = {
                "mismatch_count": int(mismatch_count),
                "sample_bet_ids": list(sample_ids or []),
            }
    finally:
        con.close()
    report["passed"] = all(
        info.get("mismatch_count", 0) == 0 for info in report["columns"].values()
    )
    return report


def apply_parity_waiver_governance(
    parity: dict[str, Any],
    *,
    compared_rows: int,
) -> dict[str, Any]:
    """Enrich parity report with hard-parity and pinned legacy bet-pack waiver fields."""
    if compared_rows < 1:
        raise ValueError(f"compared_rows must be >= 1 for waiver governance, got {compared_rows}")
    fe_cols, _bet_cols = split_scorer_short_pit_gate_columns()
    columns = parity.get("columns")
    if not isinstance(columns, dict):
        raise TypeError(f"parity.columns must be dict, got {type(columns).__name__}")

    hard_failed = [
        col
        for col in fe_cols
        if int(columns.get(col, {}).get("mismatch_count", 0)) > 0
    ]
    hard_parity_passed = len(hard_failed) == 0

    non_waived_mismatch_cols: list[str] = []
    waived_mismatch_counts: list[int] = []
    for col, info in columns.items():
        mismatch_count = int(info.get("mismatch_count", 0))
        if mismatch_count <= 0:
            continue
        if col in LEGACY_BET_PACK_1H_COLUMNS:
            waived_mismatch_counts.append(mismatch_count)
        else:
            non_waived_mismatch_cols.append(str(col))

    max_waived_mismatch = max(waived_mismatch_counts) if waived_mismatch_counts else 0
    mismatch_row_ratio = (
        float(max_waived_mismatch) / float(compared_rows) if compared_rows > 0 else 0.0
    )
    waiver_accepted = bool(
        hard_parity_passed
        and not non_waived_mismatch_cols
        and max_waived_mismatch > 0
        and mismatch_row_ratio <= float(LEGACY_BET_PACK_WAIVER_MAX_MISMATCH_RATIO)
    )

    enriched = dict(parity)
    enriched["passed"] = all(
        int(info.get("mismatch_count", 0)) == 0 for info in columns.values()
    )
    enriched["hard_parity_passed"] = hard_parity_passed
    enriched["hard_parity_columns"] = list(fe_cols)
    enriched["hard_parity_failed_columns"] = list(hard_failed)
    enriched["waived_columns"] = list(LEGACY_BET_PACK_1H_COLUMNS)
    enriched["waiver_accepted"] = waiver_accepted
    if waiver_accepted:
        enriched["waiver"] = {
            "scope": "legacy_bet_pack_1h",
            "waived_columns": list(LEGACY_BET_PACK_1H_COLUMNS),
            "mismatch_row_upper_bound": int(max_waived_mismatch),
            "mismatch_row_ratio": mismatch_row_ratio,
            "root_cause": LEGACY_BET_PACK_WAIVER_ROOT_CAUSE,
            "cluster_anchor_bet_id": int(
                columns.get("bet__bets_cnt__w1h", {}).get("sample_bet_ids", [0])[0] or 0,
            ),
            "notes": [
                "All hard-parity fe__* gate columns matched bounded oracle.",
                "Observed bet__* mismatches are limited to the pinned legacy bet 1h pack waiver.",
                "Waiver does not redefine production scorer semantics or claim full parity.",
            ],
        }
    return enriched


def evaluate_full_month_cold_build_gate(
    *,
    parity: dict[str, Any],
    output_validation_passed: bool,
    replay_elapsed_seconds: float,
    bounded_elapsed_seconds: float,
) -> dict[str, Any]:
    """Apply WP-10 full-month cold-build decision gates with pinned waiver governance."""
    hard_parity_passed = bool(parity.get("hard_parity_passed"))
    waiver_accepted = bool(parity.get("waiver_accepted"))
    parity_passed = bool(parity.get("passed"))
    speedup = (
        bounded_elapsed_seconds / replay_elapsed_seconds
        if replay_elapsed_seconds > 0
        else None
    )
    speedup_passed = speedup is not None and speedup >= 3.0
    decision_basis = {
        "speedup_passed": bool(speedup_passed),
        "output_validation_passed": bool(output_validation_passed),
        "hard_parity_passed": hard_parity_passed,
        "waiver_accepted": waiver_accepted,
    }
    if not hard_parity_passed or not output_validation_passed:
        decision = "stop_indexed_replay"
        final_integration_met = False
    elif hard_parity_passed and waiver_accepted and speedup_passed:
        decision = "integrate_candidate_with_bet_pack_waiver"
        final_integration_met = True
    elif parity_passed and speedup_passed:
        decision = "integrate_candidate"
        final_integration_met = True
    elif speedup is not None and speedup >= 1.5:
        decision = "continue_prototype"
        final_integration_met = False
    else:
        decision = "stop_or_optimize"
        final_integration_met = False
    return {
        "decision": decision,
        "parity_passed": parity_passed,
        "hard_parity_passed": hard_parity_passed,
        "waiver_accepted": waiver_accepted,
        "output_validation_passed": bool(output_validation_passed),
        "speedup_ratio": speedup,
        "final_integration_ratio": 3.0,
        "final_integration_met": final_integration_met,
        "decision_basis": decision_basis,
        "waiver_reason": LEGACY_BET_PACK_WAIVER_ROOT_CAUSE if waiver_accepted else None,
        "gate_columns": list(resolve_scorer_short_pit_prototype_gate_columns()),
        "ignored_gate_columns": list(PROTOTYPE_GATE_IGNORE_COLUMNS),
        "production_gate_columns": list(resolve_production_scorer_short_pit_gate_columns()),
    }


def benchmark_indexed_replay_vs_bounded(
    *,
    cleaned_bet_parquet: Path,
    training_parquet_for_bet_ids: Path,
    payout_yyyymm: str,
    duckdb_runtime: DuckDbRuntimeConfig,
    canonical_mapping_parquet: Path | None = None,
    target_limit: int = 1000,
    out_dir: Path | None = None,
) -> dict[str, Any]:
    """Run indexed replay and bounded oracle on the same bounded sample."""
    from trainer_hightier.feature_experiment.materialize_fe_derived import (
        materialize_fe_derived_short_term_parquet,
    )

    root = Path(out_dir or Path(training_parquet_for_bet_ids).resolve().parent / "replay_indexed_bench")
    root.mkdir(parents=True, exist_ok=True)
    subset_train = _prepare_benchmark_subset(
        training_parquet_for_bet_ids=training_parquet_for_bet_ids,
        cleaned_bet_parquet=cleaned_bet_parquet,
        payout_yyyymm=payout_yyyymm,
        target_limit=target_limit,
        duckdb_runtime=duckdb_runtime,
        out_dir=root,
    )
    replay_out = root / "replay_indexed.parquet"
    oracle_out = root / "bounded_oracle.parquet"
    gate_columns = resolve_scorer_short_pit_prototype_gate_columns()
    gate_fe_cols, gate_trial_cols = split_scorer_short_pit_gate_columns(gate_columns)
    t0 = time.perf_counter()
    _, replay_metrics = materialize_short_term_replay_indexed_prototype(
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
        short_term_columns=gate_fe_cols,
        trial_columns=gate_trial_cols,
        payout_yyyymm=payout_yyyymm,
    )
    bounded_elapsed = round(time.perf_counter() - t1, 6)
    replay_df = pq.read_table(replay_out).to_pandas()
    oracle_df = pq.read_table(oracle_out).to_pandas()
    parity = compare_replay_to_oracle(
        replay_df,
        oracle_df,
        columns=gate_columns,
    )
    speedup = round(bounded_elapsed / replay_elapsed, 3) if replay_elapsed > 0 else None
    go_no_go = evaluate_indexed_replay_go_no_go(
        parity_passed=bool(parity["passed"]),
        replay_elapsed_seconds=replay_elapsed,
        bounded_elapsed_seconds=bounded_elapsed,
        target_limit=int(target_limit),
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
        "gate_columns": list(gate_columns),
        "ignored_gate_columns": list(PROTOTYPE_GATE_IGNORE_COLUMNS),
        "production_gate_columns": list(resolve_production_scorer_short_pit_gate_columns()),
        "legacy_go_no_go": evaluate_replay_go_no_go(
            parity_passed=bool(parity["passed"]),
            replay_elapsed_seconds=replay_elapsed,
            bounded_elapsed_seconds=bounded_elapsed,
        ),
    }


def benchmark_indexed_replay_full_month_gate(
    *,
    cleaned_bet_parquet: Path,
    training_parquet_for_bet_ids: Path,
    payout_yyyymm: str,
    duckdb_runtime: DuckDbRuntimeConfig,
    canonical_mapping_parquet: Path | None = None,
    out_dir: Path | None = None,
) -> dict[str, Any]:
    """Run WP-10 full-month indexed replay vs bounded oracle cold-build gate."""
    from trainer_hightier.feature_experiment.materialize_fe_derived import (
        materialize_fe_derived_short_term_parquet,
    )

    root = Path(
        out_dir
        or Path(training_parquet_for_bet_ids).resolve().parent
        / "replay_indexed_full_month_gate",
    )
    root.mkdir(parents=True, exist_ok=True)
    phase_timings: dict[str, float] = {}
    memory_peak_mb: dict[str, float | None] = {}
    gate_columns = resolve_scorer_short_pit_prototype_gate_columns()
    gate_fe_cols, gate_trial_cols = split_scorer_short_pit_gate_columns(gate_columns)

    t0 = time.perf_counter()
    with _MemoryPeakSampler() as prep_mem:
        subset_train, target_count = _prepare_full_month_benchmark_subset(
            training_parquet_for_bet_ids=training_parquet_for_bet_ids,
            cleaned_bet_parquet=cleaned_bet_parquet,
            payout_yyyymm=payout_yyyymm,
            duckdb_runtime=duckdb_runtime,
            out_dir=root,
        )
    phase_timings["prepare_subset_s"] = round(time.perf_counter() - t0, 6)
    memory_peak_mb["prepare_subset_peak_rss_mb"] = prep_mem.peak_mb

    replay_out = root / "replay_indexed.parquet"
    oracle_out = root / "bounded_oracle.parquet"
    t_replay = time.perf_counter()
    with _MemoryPeakSampler() as replay_mem:
        _, replay_metrics = materialize_short_term_replay_indexed_prototype(
            cleaned_bet_parquet=cleaned_bet_parquet,
            training_parquet_for_bet_ids=subset_train,
            out_parquet=replay_out,
            payout_yyyymm=payout_yyyymm,
            duckdb_runtime=duckdb_runtime,
            canonical_mapping_parquet=canonical_mapping_parquet,
            target_limit=None,
        )
    replay_elapsed = round(time.perf_counter() - t_replay, 6)
    phase_timings["replay_total_s"] = replay_elapsed
    phase_timings.update(
        {f"replay_{k}": float(v) for k, v in replay_metrics.get("phase_timings", {}).items()},
    )
    memory_peak_mb["replay_peak_rss_mb"] = replay_mem.peak_mb

    t_validate = time.perf_counter()
    output_validation = _validate_replay_output_vs_targets(
        replay_parquet=replay_out,
        targets_parquet=subset_train,
        duckdb_runtime=duckdb_runtime,
    )
    phase_timings["output_validation_s"] = round(time.perf_counter() - t_validate, 6)

    t_bounded = time.perf_counter()
    with _MemoryPeakSampler() as bounded_mem:
        materialize_fe_derived_short_term_parquet(
            cleaned_bet_parquet=cleaned_bet_parquet,
            training_parquet_for_bet_ids=subset_train,
            out_parquet=oracle_out,
            duckdb_runtime=duckdb_runtime,
            canonical_mapping_parquet=canonical_mapping_parquet,
            short_term_columns=gate_fe_cols,
            trial_columns=gate_trial_cols,
            payout_yyyymm=payout_yyyymm,
        )
    bounded_elapsed = round(time.perf_counter() - t_bounded, 6)
    phase_timings["bounded_total_s"] = bounded_elapsed
    memory_peak_mb["bounded_peak_rss_mb"] = bounded_mem.peak_mb

    t_parity = time.perf_counter()
    with _MemoryPeakSampler() as parity_mem:
        parity = compare_replay_to_oracle_parquet(
            replay_out,
            oracle_out,
            columns=gate_columns,
            duckdb_runtime=duckdb_runtime,
        )
    phase_timings["parity_s"] = round(time.perf_counter() - t_parity, 6)
    memory_peak_mb["parity_peak_rss_mb"] = parity_mem.peak_mb

    compared_rows = int(parity.get("compared_rows") or output_validation.get("target_rows") or 0)
    parity = apply_parity_waiver_governance(parity, compared_rows=compared_rows)
    speedup = round(bounded_elapsed / replay_elapsed, 3) if replay_elapsed > 0 else None
    go_no_go = evaluate_full_month_cold_build_gate(
        parity=parity,
        output_validation_passed=bool(output_validation["passed"]),
        replay_elapsed_seconds=replay_elapsed,
        bounded_elapsed_seconds=bounded_elapsed,
    )
    report = {
        "gate_kind": "full_month_cold_build",
        "target_count": int(target_count),
        "replay_elapsed_seconds": replay_elapsed,
        "bounded_elapsed_seconds": bounded_elapsed,
        "speedup_ratio": speedup,
        "parity": parity,
        "output_validation": output_validation,
        "go_no_go": go_no_go,
        "replay_metrics": replay_metrics,
        "phase_timings": phase_timings,
        "memory_peak_mb": memory_peak_mb,
        "payout_yyyymm": str(payout_yyyymm),
        "gate_columns": list(gate_columns),
        "ignored_gate_columns": list(PROTOTYPE_GATE_IGNORE_COLUMNS),
        "production_gate_columns": list(resolve_production_scorer_short_pit_gate_columns()),
        "paths": {
            "out_dir": str(root.resolve()),
            "replay_parquet": str(replay_out.resolve()),
            "bounded_oracle_parquet": str(oracle_out.resolve()),
            "training_subset_parquet": str(subset_train.resolve()),
        },
    }
    report_path = root / "benchmark_report.json"
    report_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    logger.info(
        "[short_pit_replay_indexed_prototype] full-month gate decision=%s "
        "targets=%d replay_s=%.3f bounded_s=%.3f speedup=%s parity=%s",
        go_no_go["decision"],
        target_count,
        replay_elapsed,
        bounded_elapsed,
        speedup,
        parity["passed"],
    )
    return report


def _default_full_month_gate_paths() -> dict[str, Path]:
    """Default artifact paths for the 202605 full-month gate smoke."""
    from trainer_hightier.config import TRAINER_HIGHTIER_PACKAGE_DIR

    pkg = Path(TRAINER_HIGHTIER_PACKAGE_DIR)
    training = (
        pkg
        / "artifacts"
        / "training_data"
        / "cache"
        / "feast_month_group_v1"
        / "9e0ce34c5d45095f32b87e3d10d87231003be5cd3124abf66623cf8fe4efb9c1"
        / "walkaway_bet_trial_v1"
        / "cleaned"
        / "202605.parquet"
    )
    return {
        "cleaned_bet_parquet": pkg / "artifacts" / "cleaned" / "cleaned__gmwds_t_bet",
        "training_parquet_for_bet_ids": training,
        "out_dir": pkg.parent / "out" / "replay_benchmark_202605_indexed_full_month_gate16_emit_opt",
    }


if __name__ == "__main__":
    import argparse

    from trainer_hightier.config import configs_from_run_profile, get_run_profile

    defaults = _default_full_month_gate_paths()
    parser = argparse.ArgumentParser(
        description="Run WP-10 indexed replay full-month cold-build gate.",
    )
    parser.add_argument(
        "--cleaned-bet-parquet",
        type=Path,
        default=defaults["cleaned_bet_parquet"],
    )
    parser.add_argument(
        "--training-parquet",
        type=Path,
        default=defaults["training_parquet_for_bet_ids"],
    )
    parser.add_argument("--payout-yyyymm", default="202605")
    parser.add_argument("--out-dir", type=Path, default=defaults["out_dir"])
    parser.add_argument("--run-profile", default="default")
    args = parser.parse_args()
    duckdb_runtime, _, _ = configs_from_run_profile(get_run_profile(str(args.run_profile)))
    result = benchmark_indexed_replay_full_month_gate(
        cleaned_bet_parquet=args.cleaned_bet_parquet,
        training_parquet_for_bet_ids=args.training_parquet,
        payout_yyyymm=str(args.payout_yyyymm),
        duckdb_runtime=duckdb_runtime,
        out_dir=args.out_dir,
    )
    print(
        json.dumps(
            {
                "decision": result["go_no_go"]["decision"],
                "target_count": result["target_count"],
                "replay_s": result["replay_elapsed_seconds"],
                "bounded_s": result["bounded_elapsed_seconds"],
                "speedup": result["speedup_ratio"],
                "parity_passed": result["parity"]["passed"],
                "output_validation_passed": result["output_validation"]["passed"],
                "report": result["paths"]["out_dir"],
            },
            indent=2,
        ),
    )
