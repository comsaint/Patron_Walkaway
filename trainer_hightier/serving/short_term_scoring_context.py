"""Unified short-term PIT scoring context (train materialize, parity replay, production scorer)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

import numpy as np
import pandas as pd

from trainer_hightier.config import (
    DuckDbRuntimeConfig,
    HightierServingConfig,
    SHORT_TERM_TRIAL_BET_COLUMNS,
    default_hightier_serving_config,
)
from trainer_hightier.feature_experiment.materialize_fe_derived import (
    compute_fe_derived_features_from_pool,
)

DEFAULT_EXPAND_CANONICAL_ALIASES: Final[bool] = False

_BATCH_SORT_COLUMNS: Final[tuple[str, str]] = ("payout_complete_dtm", "bet_id")


@dataclass(frozen=True)
class ShortTermScoringContext:
    """Shared pool / batch policy for bounded short-term PIT."""

    expand_canonical_aliases: bool = DEFAULT_EXPAND_CANONICAL_ALIASES
    batch_size: int = 2000

    def __post_init__(self) -> None:
        if self.batch_size < 1:
            raise ValueError(f"batch_size must be >= 1, got {self.batch_size}")


def default_short_term_scoring_context(
    cfg: HightierServingConfig | None = None,
) -> ShortTermScoringContext:
    """Build context from serving config (``hightier_scorer_max_bets_per_cycle``)."""
    serving = cfg or default_hightier_serving_config()
    return ShortTermScoringContext(
        expand_canonical_aliases=DEFAULT_EXPAND_CANONICAL_ALIASES,
        batch_size=int(serving.hightier_scorer_max_bets_per_cycle),
    )


def split_short_term_column_names(
    columns: tuple[str, ...],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Partition columns into ``bet__`` trial pack vs short ``fe__`` derived."""
    trial_pack = set(SHORT_TERM_TRIAL_BET_COLUMNS)
    trial_cols: list[str] = []
    fe_cols: list[str] = []
    for col in columns:
        if col in trial_pack or str(col).startswith("bet__"):
            trial_cols.append(col)
        elif str(col).startswith("fe__"):
            fe_cols.append(col)
    return tuple(dict.fromkeys(trial_cols)), tuple(dict.fromkeys(fe_cols))


def sort_bets_for_scoring_batch(bets: pd.DataFrame) -> pd.DataFrame:
    """Sort scoring rows by payout time then ``bet_id`` (materialize / parity contract)."""
    if bets.empty:
        return bets
    missing = [c for c in _BATCH_SORT_COLUMNS if c not in bets.columns]
    if missing:
        raise ValueError(
            f"bets missing sort columns {missing}; got columns={list(bets.columns)}",
        )
    out = bets.copy()
    out["payout_complete_dtm"] = pd.to_datetime(out["payout_complete_dtm"], errors="coerce", utc=True)
    out["bet_id"] = pd.to_numeric(out["bet_id"], errors="coerce")
    return out.sort_values(list(_BATCH_SORT_COLUMNS), kind="mergesort").reset_index(drop=True)


def build_pool_from_cleaned_bets(
    bets: pd.DataFrame,
    *,
    cleaned_bet_parquet: Path,
    mapping_parquet: Path,
    serving_cfg: HightierServingConfig,
    context: ShortTermScoringContext | None = None,
) -> pd.DataFrame:
    """Bounded hot pool for offline replay / training materialize (``expand=False`` default)."""
    from trainer_hightier.serving.offline_serving_backtest import build_pool_from_cleaned_parquet

    ctx = context or default_short_term_scoring_context(serving_cfg)
    return build_pool_from_cleaned_parquet(
        bets,
        cleaned_root=cleaned_bet_parquet,
        cfg=serving_cfg,
        mapping_parquet=mapping_parquet,
        expand_canonical_aliases=ctx.expand_canonical_aliases,
    )


def build_short_term_features_for_batch(
    bets_batch: pd.DataFrame,
    *,
    cleaned_bet_parquet: Path,
    mapping_parquet: Path,
    serving_cfg: HightierServingConfig,
    duckdb_runtime: DuckDbRuntimeConfig,
    fe_columns: tuple[str, ...],
    trial_columns: tuple[str, ...] = SHORT_TERM_TRIAL_BET_COLUMNS,
    context: ShortTermScoringContext | None = None,
) -> pd.DataFrame:
    """Compute short-layer ``bet__*`` and ``fe__*`` for one training / parity batch."""
    from trainer_hightier.serving.feature_builder import (
        attach_canonical_id,
        attach_synthetic_etl_and_prediction_visible,
        attach_trial_bet_behavior_1h,
    )

    if bets_batch.empty:
        return pd.DataFrame(columns=["bet_id", *trial_columns, *fe_columns])
    work = sort_bets_for_scoring_batch(bets_batch)
    work["__etl_insert_Dtm"] = pd.to_datetime(work["payout_complete_dtm"], errors="coerce", utc=True)
    pool = build_pool_from_cleaned_bets(
        work,
        cleaned_bet_parquet=cleaned_bet_parquet,
        mapping_parquet=mapping_parquet,
        serving_cfg=serving_cfg,
        context=context,
    )
    pool = attach_canonical_id(pool, mapping_parquet=mapping_parquet)
    staged = attach_synthetic_etl_and_prediction_visible(work)
    staged = attach_canonical_id(staged, mapping_parquet=mapping_parquet)
    staged = attach_trial_bet_behavior_1h(staged, pool, duckdb_runtime=duckdb_runtime)
    scoring_bounds = None
    if fe_columns:
        from trainer_hightier.serving.scorer import compute_scoring_bounds_for_bets

        bound_cols = ["bet_id", "player_id", "canonical_id", "payout_complete_dtm"]
        if "gaming_day" in staged.columns:
            bound_cols.append("gaming_day")
        scoring_bounds = compute_scoring_bounds_for_bets(
            staged.loc[:, bound_cols],
            cfg=serving_cfg,
        )
    fe_part = (
        compute_fe_derived_features_from_pool(
            pool,
            staged["bet_id"],
            scoring_bounds=scoring_bounds,
            duckdb_runtime=duckdb_runtime,
        )
        if fe_columns
        else pd.DataFrame({"bet_id": staged["bet_id"]})
    )
    out = pd.DataFrame({"bet_id": pd.to_numeric(staged["bet_id"], errors="coerce")})
    for col in trial_columns:
        out[col] = staged[col].to_numpy()
    if fe_columns and not fe_part.empty:
        fe_aligned = out[["bet_id"]].merge(
            fe_part.loc[:, ["bet_id", *fe_columns]],
            on="bet_id",
            how="left",
        )
        for col in fe_columns:
            if col not in fe_aligned.columns:
                raise ValueError(
                    f"bounded fe__ materialization missing column {col!r}; got {list(fe_part.columns)}",
                )
            out[col] = fe_aligned[col].to_numpy()
    elif fe_columns:
        for col in fe_columns:
            out[col] = np.nan
    return out


def attach_live_short_term_pit(
    staged: pd.DataFrame,
    pool: pd.DataFrame,
    *,
    short_columns: tuple[str, ...],
    duckdb_runtime: DuckDbRuntimeConfig | None = None,
) -> pd.DataFrame:
    """Production scorer path: trial 1h + bounded short ``fe__*`` on an existing hot pool."""
    from trainer_hightier.serving.feature_builder import (
        attach_short_term_pit_features,
        attach_trial_bet_behavior_1h,
    )

    if staged.empty:
        return staged
    out = attach_trial_bet_behavior_1h(staged, pool, duckdb_runtime=duckdb_runtime)
    fe_cols = tuple(c for c in short_columns if str(c).startswith("fe__"))
    if fe_cols:
        out = attach_short_term_pit_features(out, pool, columns=fe_cols)
    return out
