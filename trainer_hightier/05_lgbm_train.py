"""Step 5: train one LightGBM on Step 4 split Parquets with optional Optuna.

No standalone CLI — invoked from :mod:`trainer_hightier.trainer`. Reads
``train.parquet`` / ``val.parquet`` / ``test.parquet`` under the Step 4 splits
directory; picks a validation threshold under ``HighTierObjectiveConfig`` (default:
alert-band precision at 1–2 alerts/hour); writes ``model.pkl`` under the given ``output_dir`` (bundle dir when called from ``run_training``).
``training_metrics.json`` is written when ``persist_training_metrics=True`` (feature experiments);
main trainer passes ``False`` and uses :class:`~trainer_hightier.reporting.writer.BundleReportWriter`.

Numeric prefix matches steps 1–4; use :func:`importlib.import_module` if importing
from code.
"""

from __future__ import annotations

import json
import logging
import math
import pickle
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import duckdb
import lightgbm as lgb
import numpy as np
import optuna
from optuna.trial import TrialState
import pandas as pd
import pyarrow.parquet as pq
from trainer_hightier.evaluation.alert_band_objective import (
    alert_band_metrics_block,
    evaluate_alert_band_on_candidates,
)
from trainer_hightier.evaluation.metrics_blocks import (
    metrics_at_threshold,
    split_metrics_block,
)
from trainer_hightier.evaluation.player_alert_policy import (
    build_player_alert_policy_metadata,
    operational_simulated_metrics_block,
)

from trainer_hightier.config import (
    ALERT_HORIZON_MIN,
    DuckDbRuntimeConfig,
    HighTierObjectiveConfig,
    OptunaFloatParamRange,
    OptunaIntParamRange,
    Step5OptunaSearchConfig,
    Step5TrainConfig,
)
from trainer_hightier.utils.duckdb_runtime import apply_duckdb_runtime_pragmas

logger = logging.getLogger(__name__)

_PACKAGE_ROOT = Path(__file__).resolve().parent

PAYOUT_TS_COLUMN: Final[str] = "payout_complete_dtm"
LABEL_COLUMN: Final[str] = "walkaway_label"
PLAYER_ID_COLUMN: Final[str] = "player_id"
GAME_ID_COLUMN: Final[str] = "game_id"
BET_ID_COLUMN: Final[str] = "bet_id"
ALERT_TS_COLUMN: Final[str] = "alert_ts"
CAT_COLUMNS: Final[frozenset[str]] = frozenset({"bet_type", "type_of_bet"})
STEP5_GROUP_COLUMNS: Final[frozenset[str]] = frozenset({PLAYER_ID_COLUMN, GAME_ID_COLUMN})
EVALUATION_GRAIN_PLAYER_GAME: Final[str] = "player_game"
PLAYER_GAME_SCORE_AGGREGATION: Final[str] = "top3_mean"

DEFAULT_MODEL_FILENAME: Final[str] = "model.pkl"
_VAL_ALERTS_PER_HR_SOFT_CAP: Final[float] = 3.0
_VAL_ALERTS_PENALTY_WEIGHT: Final[float] = 0.005
DEFAULT_METRICS_FILENAME: Final[str] = "training_metrics.json"


@dataclass(frozen=True)
class ThresholdPickResult:
    """Operating point from validation-score threshold search."""

    threshold: float
    feasible: bool
    precision: float
    recall: float
    alert_count: int
    n_samples: int


@dataclass(frozen=True)
class Step5Result:
    """Paths and metrics produced by :func:`train_lgbm_from_splits`."""

    model_path: Path
    metrics_path: Path
    report: dict[str, Any]
    threshold: float


@dataclass(frozen=True)
class PlayerGameAggregationResult:
    """Bet rows aggregated to ``player_id + game_id`` for evaluation."""

    y_true: np.ndarray
    scores: np.ndarray
    excluded_bets: int
    player_game_count: int
    bet_count: int
    candidates: pd.DataFrame


# Backward-compatible aliases for unit tests.
_metrics_at_threshold = metrics_at_threshold
_split_metrics_block = split_metrics_block


def split_window_hours_from_parquet(
    parquet_path: Path,
    *,
    duckdb_runtime: DuckDbRuntimeConfig,
) -> float | None:
    """Hours between min/max ``payout_complete_dtm`` on a split (trainer parity).

    Mirrors ``trainer.training.trainer._split_window_hours_from_parquet_payout``
    semantics: invalid / insufficient timestamps return ``None``.
    """

    p = Path(parquet_path).resolve()
    if not p.is_file():
        return None
    pe = str(p).replace("'", "''")
    con = duckdb.connect(database=":memory:")
    try:
        apply_duckdb_runtime_pragmas(con, duckdb_runtime)
        row = con.execute(
            f"SELECT min({PAYOUT_TS_COLUMN}) AS mn, max({PAYOUT_TS_COLUMN}) AS mx "
            f"FROM read_parquet('{pe}')",
        ).fetchone()
    except Exception:
        return None
    finally:
        con.close()
    if row is None or row[0] is None or row[1] is None:
        return None
    mn = pd.to_datetime(row[0], errors="coerce")
    mx = pd.to_datetime(row[1], errors="coerce")
    if pd.isna(mn) or pd.isna(mx):
        return None
    span_sec = float((mx - mn).total_seconds())
    if not math.isfinite(span_sec) or span_sec <= 0.0:
        return None
    return span_sec / 3600.0


def _validate_parquet_schema(parquet_path: Path, required: frozenset[str]) -> None:
    """Raise if ``parquet_path`` is missing any ``required`` column names."""

    names = frozenset(pq.ParquetFile(Path(parquet_path).resolve()).schema_arrow.names)
    missing = sorted(required.difference(names))
    if missing:
        raise ValueError(
            f"Step 5 schema gate failed: missing columns {missing}; "
            f"expected at least {sorted(required)}. path={parquet_path!r}, got {sorted(names)!r}.",
        )


def _load_split_frame(parquet_path: Path, *, feature_columns: tuple[str, ...]) -> pd.DataFrame:
    cols = list(feature_columns) + [
        LABEL_COLUMN,
        PAYOUT_TS_COLUMN,
        PLAYER_ID_COLUMN,
        GAME_ID_COLUMN,
        BET_ID_COLUMN,
    ]
    _validate_parquet_schema(parquet_path, frozenset(cols))
    return pd.read_parquet(Path(parquet_path).resolve(), columns=cols)


def _empty_player_game_candidates() -> pd.DataFrame:
    """Return an empty player-game candidate frame with the expected schema."""

    return pd.DataFrame(
        columns=[
            PLAYER_ID_COLUMN,
            GAME_ID_COLUMN,
            "player_game_score",
            "player_game_label",
            ALERT_TS_COLUMN,
            BET_ID_COLUMN,
        ],
    )


def _coerce_group_id_series(series: pd.Series) -> pd.Series:
    """Coerce identifier columns to nullable ``Int64`` for stable player-game grouping."""

    if pd.api.types.is_integer_dtype(series.dtype):
        return series.astype("Int64")
    return pd.to_numeric(series, errors="coerce").astype("Int64")


def top3_mean_bet_scores(values: np.ndarray) -> float:
    """Return mean of the highest up-to-3 bet scores in one player-game group."""

    vals = np.asarray(values, dtype=np.float64).reshape(-1)
    vals = vals[np.isfinite(vals)]
    if vals.size == 0:
        return float("nan")
    top = np.sort(vals)[-min(3, int(vals.size)) :]
    return float(np.mean(top))


def aggregate_bets_to_player_game(
    df: pd.DataFrame,
    scores: np.ndarray,
    *,
    split_name: str,
) -> PlayerGameAggregationResult:
    """Aggregate bet-level scores/labels to one row per ``player_id + game_id``.

    Score uses ``top3_mean`` (mean of highest up-to-3 bet scores); label uses
    ``max`` (any positive within the game). Representative ``alert_ts`` / ``bet_id``
    come from the chronologically last bet in the group. Rows with null keys or
    non-finite scores are excluded with a warning.
    """

    if len(df) != int(len(scores)):
        raise ValueError(
            f"aggregate_bets_to_player_game: df length {len(df)} != scores length {len(scores)} "
            f"(split={split_name!r})",
        )
    need_cols = STEP5_GROUP_COLUMNS | {
        LABEL_COLUMN,
        PAYOUT_TS_COLUMN,
        BET_ID_COLUMN,
    }
    for col in need_cols:
        if col not in df.columns:
            raise ValueError(
                f"aggregate_bets_to_player_game missing column {col!r}; "
                f"split={split_name!r}, got {list(df.columns)!r}",
            )
    work = df[
        [PLAYER_ID_COLUMN, GAME_ID_COLUMN, LABEL_COLUMN, PAYOUT_TS_COLUMN, BET_ID_COLUMN]
    ].copy()
    work["_score"] = np.asarray(scores, dtype=np.float64).reshape(-1)
    work[PLAYER_ID_COLUMN] = _coerce_group_id_series(work[PLAYER_ID_COLUMN])
    work[GAME_ID_COLUMN] = _coerce_group_id_series(work[GAME_ID_COLUMN])
    lbl = pd.to_numeric(work[LABEL_COLUMN], errors="coerce")
    alert_ts = pd.to_datetime(work[PAYOUT_TS_COLUMN], errors="coerce")
    valid = (
        work[PLAYER_ID_COLUMN].notna()
        & work[GAME_ID_COLUMN].notna()
        & lbl.notna()
        & alert_ts.notna()
        & np.isfinite(work["_score"].to_numpy())
    )
    excluded = int((~valid).sum())
    if excluded > 0:
        logger.warning(
            "Step 5 %s: excluded %d bet rows with null player_id/game_id/label/"
            "payout_complete_dtm or non-finite score",
            split_name,
            excluded,
        )
    work = work.loc[valid].copy()
    if work.empty:
        return PlayerGameAggregationResult(
            y_true=np.array([], dtype=np.int8),
            scores=np.array([], dtype=np.float64),
            excluded_bets=excluded,
            player_game_count=0,
            bet_count=0,
            candidates=_empty_player_game_candidates(),
        )
    work["_bet_id_sort"] = pd.to_numeric(work[BET_ID_COLUMN], errors="coerce").fillna(-1)
    work = work.sort_values(
        by=[PLAYER_ID_COLUMN, GAME_ID_COLUMN, PAYOUT_TS_COLUMN, "_bet_id_sort"],
        ascending=[True, True, True, True],
        kind="mergesort",
    )

    def _agg_top3_mean(series: pd.Series) -> float:
        return top3_mean_bet_scores(series.to_numpy())

    grouped = (
        work.groupby([PLAYER_ID_COLUMN, GAME_ID_COLUMN], as_index=False, dropna=True)
        .agg(
            player_game_score=("_score", _agg_top3_mean),
            player_game_label=(LABEL_COLUMN, "max"),
            bet_count=(LABEL_COLUMN, "count"),
        )
    )
    rep = work.groupby([PLAYER_ID_COLUMN, GAME_ID_COLUMN], as_index=False, dropna=True).last()
    candidates = grouped.merge(
        rep[[PLAYER_ID_COLUMN, GAME_ID_COLUMN, PAYOUT_TS_COLUMN, BET_ID_COLUMN]],
        on=[PLAYER_ID_COLUMN, GAME_ID_COLUMN],
        how="left",
    )
    candidates = candidates.rename(columns={PAYOUT_TS_COLUMN: ALERT_TS_COLUMN})
    y_pg = np.asarray(grouped["player_game_label"], dtype=np.int8)
    s_pg = np.asarray(grouped["player_game_score"], dtype=np.float64)
    return PlayerGameAggregationResult(
        y_true=y_pg,
        scores=s_pg,
        excluded_bets=excluded,
        player_game_count=int(len(grouped)),
        bet_count=int(len(work)),
        candidates=candidates,
    )


def _prepare_xy(df: pd.DataFrame, *, feature_columns: tuple[str, ...]) -> tuple[pd.DataFrame, np.ndarray]:
    """Return feature frame (with category dtypes) and binary labels ``0/1``."""

    y_raw = pd.to_numeric(df[LABEL_COLUMN], errors="coerce")
    if y_raw.isna().any():
        bad = int(y_raw.isna().sum())
        raise ValueError(f"Label column {LABEL_COLUMN!r} has {bad} non-numeric/null values.")
    y = np.asarray(y_raw, dtype=np.int8)
    uniq = np.unique(y)
    if not np.isin(uniq, [0, 1]).all():
        raise ValueError(f"Labels must be binary {{0,1}}; got unique {uniq.tolist()}")
    from trainer_hightier.serving.feature_builder import prepare_lgbm_feature_matrix

    X = prepare_lgbm_feature_matrix(
        df,
        feature_columns=feature_columns,
        categorical_columns=CAT_COLUMNS,
    )
    return X, y


def _resolve_objective_config(
    objective: HighTierObjectiveConfig | None,
    objective_min_precision: float,
) -> HighTierObjectiveConfig:
    """Merge explicit objective config with legacy ``objective_min_precision`` override."""

    if objective is not None:
        return objective
    return HighTierObjectiveConfig(min_precision=float(objective_min_precision))


def _pick_validation_threshold(
    val_pg: PlayerGameAggregationResult,
    *,
    objective_cfg: HighTierObjectiveConfig,
    val_window_hours: float | None,
) -> tuple[ThresholdPickResult, float, dict[str, Any] | None]:
    """Return threshold pick, Optuna scalar score, and optional band metadata."""

    if objective_cfg.selection_policy == "min_precision":
        pick = pick_threshold_precision_floor(
            val_pg.y_true,
            val_pg.scores,
            min_precision=float(objective_cfg.min_precision),
        )
        objective_val = _val_optuna_objective_score(
            pick,
            val_window_hours=val_window_hours,
            alert_count_at_pick=int(pick.alert_count),
            objective_min_precision=float(objective_cfg.min_precision),
        )
        return pick, objective_val, None

    band = evaluate_alert_band_on_candidates(
        val_pg.candidates,
        window_hours=val_window_hours,
        target_alerts_per_hour=tuple(objective_cfg.target_alerts_per_hour),
        deployment_target_alerts_per_hour=float(objective_cfg.deployment_target_alerts_per_hour),
        split_prefix="val",
    )
    deploy = next(
        (
            p
            for p in band.points
            if math.isclose(p.target_alerts_per_hour, float(objective_cfg.deployment_target_alerts_per_hour))
        ),
        band.points[0],
    )
    pick = ThresholdPickResult(
        threshold=float(band.deployment_threshold),
        feasible=True,
        precision=float(deploy.precision),
        recall=float(deploy.recall),
        alert_count=int(deploy.alerts),
        n_samples=int(len(val_pg.y_true)),
    )
    band_meta = {
        "scalar_score": float(band.scalar_score),
        "min_precision": float(band.min_precision),
        "mean_precision": float(band.mean_precision),
        "deployment_target_alerts_per_hour": float(objective_cfg.deployment_target_alerts_per_hour),
        "target_alerts_per_hour": list(objective_cfg.target_alerts_per_hour),
        "points": [
            {
                "target_alerts_per_hour": float(p.target_alerts_per_hour),
                "target_alert_count": int(p.target_alert_count),
                "threshold": float(p.threshold),
                "precision": float(p.precision),
                "recall": float(p.recall),
                "alerts": int(p.alerts),
                "alerts_per_hour": p.alerts_per_hour,
                "true_positives": int(p.true_positives),
            }
            for p in band.points
        ],
    }
    return pick, float(band.scalar_score), band_meta


def _val_optuna_objective_score(
    pick: ThresholdPickResult,
    *,
    val_window_hours: float | None,
    alert_count_at_pick: int,
    objective_min_precision: float,
) -> float:
    """Maximize recall at precision floor; penalize excessive validation alert rate."""

    if not pick.feasible:
        return float(pick.precision) - float(objective_min_precision) - 1.0
    recall = float(pick.recall)
    if val_window_hours is None or val_window_hours <= 0:
        return recall
    alerts_per_hour = float(alert_count_at_pick) / float(val_window_hours)
    penalty = max(0.0, alerts_per_hour - _VAL_ALERTS_PER_HR_SOFT_CAP) * _VAL_ALERTS_PENALTY_WEIGHT
    return recall - penalty


def pick_threshold_precision_floor(
    y_true: np.ndarray,
    scores: np.ndarray,
    *,
    min_precision: float,
) -> ThresholdPickResult:
    """Pick threshold on validation scores under precision floor rules.

    Alerts are ``scores >= threshold``. Candidates are prefixes after sorting
    scores descending at boundaries between equal-score blocks.

    When feasible (exists prefix with precision >= ``min_precision``): maximize
    recall; ties — higher precision, then higher threshold.

    When infeasible: maximize precision; ties — higher recall, then higher threshold.
    """

    y_arr = np.asarray(y_true, dtype=np.int8).reshape(-1)
    s_arr = np.asarray(scores, dtype=np.float64).reshape(-1)
    if y_arr.shape[0] != s_arr.shape[0]:
        raise ValueError(f"y_true length {y_arr.shape[0]} != scores length {s_arr.shape[0]}")
    if y_arr.size == 0:
        raise ValueError("y_true must be non-empty")
    if not np.isfinite(s_arr).all():
        raise ValueError("scores must be finite (no NaN/inf)")
    mp = float(min_precision)
    if not (0.0 < mp <= 1.0) or not math.isfinite(mp):
        raise ValueError(f"min_precision must be finite in (0,1]; got {min_precision!r}")

    pos = int(np.sum(y_arr == 1))
    n = int(len(y_arr))
    if pos == 0:
        return ThresholdPickResult(
            threshold=float("nan"),
            feasible=False,
            precision=0.0,
            recall=0.0,
            alert_count=0,
            n_samples=n,
        )

    order = np.argsort(-s_arr, kind="mergesort")
    ys = y_arr[order].astype(np.int64)
    scs = s_arr[order]
    cum_tp = np.cumsum(ys)

    candidates: list[tuple[float, int, float, float]] = []
    i = 0
    while i < n:
        thr = float(scs[i])
        j = i
        while j < n and float(scs[j]) == thr:
            j += 1
        k = j
        tp = int(cum_tp[k - 1])
        prec = tp / float(k)
        rec = tp / float(pos)
        candidates.append((thr, k, prec, rec))
        i = j

    feasible_pts = [(thr, k, prec, rec) for thr, k, prec, rec in candidates if prec >= mp - 1e-15]
    if feasible_pts:
        thr, k, prec, rec = max(feasible_pts, key=lambda t: (t[3], t[2], t[0]))
        return ThresholdPickResult(
            threshold=float(thr),
            feasible=True,
            precision=float(prec),
            recall=float(rec),
            alert_count=int(k),
            n_samples=n,
        )

    thr, k, prec, rec = max(candidates, key=lambda t: (t[2], t[3], t[0]))
    return ThresholdPickResult(
        threshold=float(thr),
        feasible=False,
        precision=float(prec),
        recall=float(rec),
        alert_count=int(k),
        n_samples=n,
    )


def _lgb_fixed_params(cfg: Step5TrainConfig, seed: int) -> dict[str, Any]:
    """Return non-tuned LightGBM kwargs from :attr:`Step5TrainConfig.lgb_fixed`."""

    fixed = cfg.lgb_fixed
    out: dict[str, Any] = {
        "objective": fixed.objective,
        "metric": fixed.metric,
        "verbosity": int(fixed.verbosity),
        "n_estimators": int(cfg.lgb_n_estimators_cap),
        "random_state": int(seed),
        "n_jobs": int(fixed.n_jobs),
    }
    device = fixed.device
    if device is not None and str(device).strip():
        out["device"] = str(device).strip()
        if str(device).strip().lower() == "gpu":
            out["gpu_device_id"] = int(fixed.gpu_device_id)
    return out


def _baseline_tunable_params(cfg: Step5TrainConfig) -> dict[str, Any]:
    """Return baseline tunable LightGBM kwargs from ``baseline_*`` config fields."""

    return {
        "learning_rate": float(cfg.baseline_learning_rate),
        "num_leaves": int(cfg.baseline_num_leaves),
        "max_depth": int(cfg.baseline_max_depth),
        "min_child_samples": int(cfg.baseline_min_child_samples),
        "subsample": float(cfg.baseline_subsample),
        "colsample_bytree": float(cfg.baseline_colsample_bytree),
        "reg_alpha": float(cfg.baseline_reg_alpha),
        "reg_lambda": float(cfg.baseline_reg_lambda),
    }


def _suggest_float_param(trial: optuna.Trial, name: str, spec: OptunaFloatParamRange) -> float:
    """Sample one float hyperparameter from config bounds."""

    return float(
        trial.suggest_float(
            name,
            float(spec.low),
            float(spec.high),
            log=bool(spec.log),
        ),
    )


def _suggest_int_param(trial: optuna.Trial, name: str, spec: OptunaIntParamRange) -> int:
    """Sample one int hyperparameter from config bounds."""

    return int(
        trial.suggest_int(
            name,
            int(spec.low),
            int(spec.high),
        ),
    )


def _optuna_tunable_params(trial: optuna.Trial, search: Step5OptunaSearchConfig) -> dict[str, Any]:
    """Sample all Optuna tunable LightGBM kwargs from :class:`Step5OptunaSearchConfig`."""

    return {
        "learning_rate": _suggest_float_param(trial, "learning_rate", search.learning_rate),
        "num_leaves": _suggest_int_param(trial, "num_leaves", search.num_leaves),
        "max_depth": _suggest_int_param(trial, "max_depth", search.max_depth),
        "min_child_samples": _suggest_int_param(trial, "min_child_samples", search.min_child_samples),
        "subsample": _suggest_float_param(trial, "subsample", search.subsample),
        "colsample_bytree": _suggest_float_param(trial, "colsample_bytree", search.colsample_bytree),
        "reg_alpha": _suggest_float_param(trial, "reg_alpha", search.reg_alpha),
        "reg_lambda": _suggest_float_param(trial, "reg_lambda", search.reg_lambda),
    }


def _baseline_lgb_params(cfg: Step5TrainConfig, seed: int) -> dict[str, Any]:
    return {**_lgb_fixed_params(cfg, seed), **_baseline_tunable_params(cfg)}


def _rebuild_lgb_params_from_optuna_best(
    best_params: dict[str, Any],
    cfg: Step5TrainConfig,
    seed: int,
) -> dict[str, Any]:
    """Rebuild full LightGBM kwargs from Optuna ``best_params``."""

    return {**_lgb_fixed_params(cfg, seed), **dict(best_params)}


def _suggest_lgb_params(trial: optuna.Trial, cfg: Step5TrainConfig, seed: int) -> dict[str, Any]:
    return {**_lgb_fixed_params(cfg, seed), **_optuna_tunable_params(trial, cfg.optuna_search)}


def _optuna_trial_debug_log(study: optuna.study.Study, trial: optuna.trial.FrozenTrial) -> None:
    """Log each finished Optuna trial at DEBUG only (objective value and sampled params)."""

    logger.debug(
        "Optuna trial %s state=%s value=%s params=%s",
        trial.number,
        trial.state,
        trial.value,
        trial.params,
    )


def _optuna_stopping_reason(*, wall_sec: float, timeout_sec: float, n_trials_total: int, n_completed: int) -> str:
    """Classify why Optuna stopped (minimal enum for cross-run comparability)."""

    if n_completed < 1:
        return "no_completed_trials"
    if wall_sec >= float(timeout_sec) * 0.995:
        return "time_budget_exhausted"
    if n_trials_total > 0 and n_completed == n_trials_total:
        return "completed"
    return "unknown"


def _train_one_lgbm(
    X_tr: pd.DataFrame,
    y_tr: np.ndarray,
    X_va: pd.DataFrame,
    y_va: np.ndarray,
    hp: dict[str, Any],
    *,
    early_stopping_rounds: int,
) -> lgb.LGBMClassifier:
    clf = lgb.LGBMClassifier(**hp)
    callbacks = [
        lgb.early_stopping(stopping_rounds=int(early_stopping_rounds), verbose=False),
        lgb.log_evaluation(period=0),
    ]
    clf.fit(
        X_tr,
        y_tr,
        eval_set=[(X_va, y_va)],
        eval_metric=str(hp["metric"]),
        callbacks=callbacks,
    )
    return clf


def _refit_final_lgbm_train_plus_val(
    *,
    base_model: lgb.LGBMClassifier,
    best_hp: dict[str, Any],
    X_tr: pd.DataFrame,
    y_tr: np.ndarray,
    X_va: pd.DataFrame,
    y_va: np.ndarray,
) -> tuple[lgb.LGBMClassifier, int | None]:
    """Refit final model on train+val using best hyperparameters only."""

    refit_hp = dict(best_hp)
    best_iter_raw = getattr(base_model, "best_iteration_", None)
    best_iter = int(best_iter_raw) if best_iter_raw is not None and int(best_iter_raw) > 0 else None
    if best_iter is not None:
        refit_hp["n_estimators"] = best_iter
    X_refit = pd.concat([X_tr, X_va], axis=0, ignore_index=True)
    y_refit = np.concatenate([y_tr, y_va], axis=0)
    final_model = lgb.LGBMClassifier(**refit_hp)
    final_model.fit(X_refit, y_refit)
    return final_model, best_iter


def train_lgbm_from_splits(
    *,
    splits_dir: Path,
    duckdb_runtime: DuckDbRuntimeConfig,
    objective_min_precision: float,
    random_seed: int,
    step5: Step5TrainConfig | None = None,
    output_dir: Path,
    feature_columns: tuple[str, ...],
    persist_training_metrics: bool = True,
    train_parquet: Path | None = None,
    sample_policy_meta: dict[str, Any] | None = None,
    feature_screening_meta: dict[str, Any] | None = None,
    objective: HighTierObjectiveConfig | None = None,
) -> Step5Result:
    """Train LightGBM on Step 4 splits; optional Optuna; pick threshold on val; write artifacts.

    Args:
        feature_columns: Model input columns (excluding label / payout_ts). Required; use
            :func:`~trainer_hightier.feature_experiment.candidate_registry_loader.baseline_features_for_main_trainer`
            or full-candidate tuples from the candidate registry snapshot.
        train_parquet: Optional sampled train split; defaults to ``splits_dir/train.parquet``.
        sample_policy_meta: Optional downsampling disclosure block from
            :func:`~trainer_hightier.utils.train_negative_sampling.materialize_sampled_train_parquet`.
        feature_screening_meta: Optional screening hook disclosure from
            :func:`~trainer_hightier.utils.feature_screening_hook.resolve_step5_feature_columns`.
    """

    if not feature_columns:
        raise ValueError(
            "train_lgbm_from_splits requires non-empty feature_columns "
            "(derive from trainer_hightier/contracts/feature_candidate_registry.yaml via load_candidate_registry).",
        )
    feat_cols = tuple(feature_columns)
    cfg = step5 or Step5TrainConfig()
    objective_cfg = _resolve_objective_config(objective, float(objective_min_precision))
    sd = Path(splits_dir).resolve()
    train_p = Path(train_parquet).resolve() if train_parquet is not None else sd / "train.parquet"
    val_p = sd / "val.parquet"
    test_p = sd / "test.parquet"
    for pth in (train_p, val_p, test_p):
        if not pth.is_file():
            raise FileNotFoundError(f"Step 5 requires split parquet at {pth}")

    wh_train = split_window_hours_from_parquet(train_p, duckdb_runtime=duckdb_runtime)
    wh_val = split_window_hours_from_parquet(val_p, duckdb_runtime=duckdb_runtime)
    wh_test = split_window_hours_from_parquet(test_p, duckdb_runtime=duckdb_runtime)
    if wh_train is None:
        logger.warning(
            "Step 5 train: cannot compute %s window_hours; train_alerts_per_hour omitted.",
            PAYOUT_TS_COLUMN,
        )
    if wh_val is None:
        logger.warning(
            "Step 5 val: cannot compute %s window_hours; val_alerts_per_hour omitted.",
            PAYOUT_TS_COLUMN,
        )
    if wh_test is None:
        logger.warning(
            "Step 5 test: cannot compute %s window_hours; test_alerts_per_hour omitted.",
            PAYOUT_TS_COLUMN,
        )

    df_tr = _load_split_frame(train_p, feature_columns=feat_cols)
    df_va = _load_split_frame(val_p, feature_columns=feat_cols)
    df_te = _load_split_frame(test_p, feature_columns=feat_cols)
    X_tr, y_tr = _prepare_xy(df_tr, feature_columns=feat_cols)
    X_va, y_va = _prepare_xy(df_va, feature_columns=feat_cols)
    X_te, y_te = _prepare_xy(df_te, feature_columns=feat_cols)

    cat_cols = [c for c in feat_cols if c in CAT_COLUMNS]
    union_cats: dict[str, pd.Index] = {}
    for c in cat_cols:
        combined = pd.concat(
            [X_tr[c].astype(str), X_va[c].astype(str), X_te[c].astype(str)],
            axis=0,
            ignore_index=True,
        )
        union_cats[c] = pd.Index(pd.unique(combined))
    for c in cat_cols:
        X_tr[c] = pd.Categorical(X_tr[c], categories=union_cats[c])
        X_va[c] = pd.Categorical(X_va[c], categories=union_cats[c])
        X_te[c] = pd.Categorical(X_te[c], categories=union_cats[c])

    val_pos = int(np.sum(y_va == 1))
    if val_pos < 1 or int(np.sum(y_va == 0)) < 1:
        raise ValueError(
            f"Validation set must have at least one positive and one negative label; "
            f"got positives={val_pos}, n={len(y_va)}.",
        )

    def _evaluate_hp(hp: dict[str, Any]) -> tuple[lgb.LGBMClassifier, ThresholdPickResult, float, dict[str, Any] | None]:
        model = _train_one_lgbm(
            X_tr,
            y_tr,
            X_va,
            y_va,
            hp,
            early_stopping_rounds=int(cfg.early_stopping_rounds),
        )
        val_scores = model.predict_proba(X_va)[:, 1]
        val_pg = aggregate_bets_to_player_game(df_va, val_scores, split_name="val")
        val_pick, objective_val, band_meta = _pick_validation_threshold(
            val_pg,
            objective_cfg=objective_cfg,
            val_window_hours=wh_val,
        )
        return model, val_pick, objective_val, band_meta

    t0 = time.perf_counter()

    if cfg.skip_optuna:
        best_hp = _baseline_lgb_params(cfg, random_seed)
        model, val_pick, _obj, val_band_meta = _evaluate_hp(best_hp)
        study_summary = {
            "optuna_max_time_sec_configured": float(cfg.optuna_timeout_sec),
            "optuna_max_trials_configured": None,
            "optuna_wall_time_sec_actual": None,
            "optuna_trials_completed": 0,
            "optuna_trials_total": 0,
            "optuna_stopping_reason": "optuna_skipped",
            "optuna_n_trials": 0,
            "optuna_best_value": None,
            "optuna_best_params": {},
        }
    else:
        sampler = optuna.samplers.TPESampler(seed=int(random_seed))

        def objective(trial: optuna.Trial) -> float:
            hp = _suggest_lgb_params(trial, cfg, random_seed)
            _, _, obj_inner, _ = _evaluate_hp(hp)
            return float(obj_inner)

        study = optuna.create_study(direction="maximize", sampler=sampler)
        _prev_optuna_verbosity = optuna.logging.get_verbosity()
        try:
            optuna.logging.set_verbosity(optuna.logging.WARNING)
            t_opt0 = time.perf_counter()
            study.optimize(
                objective,
                timeout=float(cfg.optuna_timeout_sec),
                show_progress_bar=True,
                callbacks=[_optuna_trial_debug_log],
            )
            opt_wall = round(time.perf_counter() - t_opt0, 3)
        finally:
            optuna.logging.set_verbosity(_prev_optuna_verbosity)
        n_trials_total = len(study.trials)
        n_completed = int(sum(1 for tr in study.trials if tr.state == TrialState.COMPLETE))
        stop_reason = _optuna_stopping_reason(
            wall_sec=float(opt_wall),
            timeout_sec=float(cfg.optuna_timeout_sec),
            n_trials_total=n_trials_total,
            n_completed=n_completed,
        )
        study_summary = {
            "optuna_max_time_sec_configured": float(cfg.optuna_timeout_sec),
            "optuna_max_trials_configured": None,
            "optuna_wall_time_sec_actual": float(opt_wall),
            "optuna_trials_completed": n_completed,
            "optuna_trials_total": n_trials_total,
            "optuna_stopping_reason": stop_reason,
            "optuna_n_trials": n_trials_total,
        }
        try:
            study_summary["optuna_best_value"] = float(study.best_value)
            study_summary["optuna_best_params"] = dict(study.best_params)
            best_hp = _rebuild_lgb_params_from_optuna_best(study.best_params, cfg, random_seed)
        except RuntimeError:
            logger.warning("Step 5: Optuna has no completed trials; using baseline hyperparameters.")
            study_summary["optuna_best_value"] = None
            study_summary["optuna_best_params"] = {}
            study_summary["optuna_stopping_reason"] = "no_completed_trials"
            best_hp = _baseline_lgb_params(cfg, random_seed)
        model, val_pick, _, val_band_meta = _evaluate_hp(best_hp)

    final_model = model
    refit_best_iteration: int | None = None
    if bool(cfg.refit_train_plus_val):
        final_model, refit_best_iteration = _refit_final_lgbm_train_plus_val(
            base_model=model,
            best_hp=best_hp,
            X_tr=X_tr,
            y_tr=y_tr,
            X_va=X_va,
            y_va=y_va,
        )

    train_scores = final_model.predict_proba(X_tr)[:, 1]
    val_scores = final_model.predict_proba(X_va)[:, 1]
    test_scores = final_model.predict_proba(X_te)[:, 1]

    if objective_cfg.selection_policy == "min_precision" and not val_pick.feasible:
        logger.warning(
            "Step 5: no threshold achieves min_precision=%.4f on validation; reporting best achievable "
            "precision=%.4f recall=%.4f at threshold=%.6f.",
            float(objective_cfg.min_precision),
            val_pick.precision,
            val_pick.recall,
            val_pick.threshold,
        )

    thr = float(val_pick.threshold)
    pg_tr = aggregate_bets_to_player_game(df_tr, train_scores, split_name="train")
    pg_va = aggregate_bets_to_player_game(df_va, val_scores, split_name="val")
    pg_te = aggregate_bets_to_player_game(df_te, test_scores, split_name="test")
    block_tr_pg = split_metrics_block(
        "train_player_game", pg_tr.y_true, pg_tr.scores, thr, window_hours=wh_train,
    )
    block_va_pg = split_metrics_block(
        "val_player_game", pg_va.y_true, pg_va.scores, thr, window_hours=wh_val,
    )
    block_te_pg = split_metrics_block(
        "test_player_game", pg_te.y_true, pg_te.scores, thr, window_hours=wh_test,
    )
    block_tr_bl = split_metrics_block(
        "train_bet_level", y_tr, train_scores, thr, window_hours=wh_train,
    )
    block_va_bl = split_metrics_block(
        "val_bet_level", y_va, val_scores, thr, window_hours=wh_val,
    )
    block_te_bl = split_metrics_block(
        "test_bet_level", y_te, test_scores, thr, window_hours=wh_test,
    )
    block_tr_main = split_metrics_block("train", pg_tr.y_true, pg_tr.scores, thr, window_hours=wh_train)
    block_va_main = split_metrics_block("val", pg_va.y_true, pg_va.scores, thr, window_hours=wh_val)
    block_te_main = split_metrics_block("test", pg_te.y_true, pg_te.scores, thr, window_hours=wh_test)
    block_tr_op = operational_simulated_metrics_block(
        "train",
        pg_tr.candidates,
        thr,
        window_hours=wh_train,
    )
    block_va_op = operational_simulated_metrics_block(
        "val",
        pg_va.candidates,
        thr,
        window_hours=wh_val,
    )
    block_te_op = operational_simulated_metrics_block(
        "test",
        pg_te.candidates,
        thr,
        window_hours=wh_test,
    )
    band_blocks: dict[str, Any] = {}
    if objective_cfg.selection_policy == "alert_band_precision":
        band_blocks.update(
            alert_band_metrics_block(
                "val",
                pg_va.candidates,
                window_hours=wh_val,
                target_alerts_per_hour=tuple(objective_cfg.target_alerts_per_hour),
            )
        )
        band_blocks.update(
            alert_band_metrics_block(
                "test",
                pg_te.candidates,
                window_hours=wh_test,
                target_alerts_per_hour=tuple(objective_cfg.target_alerts_per_hour),
            )
        )

    elapsed = round(time.perf_counter() - t0, 3)
    policy_meta = build_player_alert_policy_metadata(cfg.player_alert_policy)
    report: dict[str, Any] = {
        "evaluation_grain": EVALUATION_GRAIN_PLAYER_GAME,
        "player_cooldown_simulated": True,
        "player_cooldown_simulated_min": int(ALERT_HORIZON_MIN),
        "player_cooldown_alert_ts_source": PAYOUT_TS_COLUMN,
        "player_game_group_key": [PLAYER_ID_COLUMN, GAME_ID_COLUMN],
        "score_aggregation": PLAYER_GAME_SCORE_AGGREGATION,
        "label_aggregation": "max",
        "step5_threshold_grain": EVALUATION_GRAIN_PLAYER_GAME,
        "step5_seconds": elapsed,
        "step5_feature_columns": list(feat_cols),
        "step5_threshold": thr,
        "step5_val_pick_feasible": val_pick.feasible,
        "step5_val_precision_at_pick": val_pick.precision,
        "step5_val_recall_at_pick": val_pick.recall,
        "step5_selection_policy": objective_cfg.selection_policy,
        "step5_target_alerts_per_hour": list(objective_cfg.target_alerts_per_hour),
        "step5_deployment_target_alerts_per_hour": float(objective_cfg.deployment_target_alerts_per_hour),
        "step5_min_precision": float(objective_cfg.min_precision),
        "step5_optuna_skipped": bool(cfg.skip_optuna),
        "step5_refit_train_plus_val": bool(cfg.refit_train_plus_val),
        "step5_refit_rows": int(len(y_tr) + len(y_va)) if bool(cfg.refit_train_plus_val) else int(len(y_tr)),
        "step5_refit_best_iteration": refit_best_iteration,
        "train_excluded_bets_player_game": pg_tr.excluded_bets,
        "val_excluded_bets_player_game": pg_va.excluded_bets,
        "test_excluded_bets_player_game": pg_te.excluded_bets,
        "train_player_game_count": pg_tr.player_game_count,
        "val_player_game_count": pg_va.player_game_count,
        "test_player_game_count": pg_te.player_game_count,
        **block_tr_main,
        **block_va_main,
        **block_te_main,
        **block_tr_pg,
        **block_va_pg,
        **block_te_pg,
        **block_tr_bl,
        **block_va_bl,
        **block_te_bl,
        **block_tr_op,
        **block_va_op,
        **block_te_op,
        **band_blocks,
        **policy_meta,
    }
    report.update(study_summary)
    if val_band_meta is not None:
        report["step5_val_alert_band"] = val_band_meta
        report["step5_alert_band_scalar_score"] = val_band_meta.get("scalar_score")
    if isinstance(sample_policy_meta, dict) and sample_policy_meta:
        report["sample_policy"] = dict(sample_policy_meta)
        report["sample_policy_fingerprint"] = sample_policy_meta.get("sample_policy_fingerprint")
        report["val_test_evaluation_unsampled"] = bool(
            sample_policy_meta.get("val_test_evaluation_unsampled", True),
        )
        if sample_policy_meta.get("enabled"):
            report["train_rows_before_sampling"] = sample_policy_meta.get("train_rows_before")
            report["train_rows_after_sampling"] = sample_policy_meta.get("train_rows_after")
            report["train_negatives_before_sampling"] = sample_policy_meta.get("train_negatives_before")
            report["train_negatives_after_sampling"] = sample_policy_meta.get("train_negatives_after")
            report["train_evaluation_sampled"] = True
        else:
            report["train_evaluation_sampled"] = False
    if isinstance(feature_screening_meta, dict) and feature_screening_meta:
        report["feature_screening"] = dict(feature_screening_meta)
        report["feature_selection_policy_fingerprint"] = feature_screening_meta.get(
            "feature_selection_policy_fingerprint",
        )
        report["feature_selection_manifest_fingerprint"] = feature_screening_meta.get(
            "feature_selection_manifest_fingerprint",
        )
        report["step5_feature_screening_enabled"] = bool(feature_screening_meta.get("enabled"))
        report["step5_selected_feature_count"] = feature_screening_meta.get("selected_feature_count")

    out_dir = Path(output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    model_path = (out_dir / DEFAULT_MODEL_FILENAME).resolve()
    with open(model_path, "wb") as f:
        pickle.dump(
            {
                "model": final_model,
                "feature_columns": list(feat_cols),
                "categorical_columns": list(CAT_COLUMNS),
                "category_categories": {c: union_cats[c].tolist() for c in cat_cols},
                "threshold": thr,
                "score_aggregation": PLAYER_GAME_SCORE_AGGREGATION,
                "selection_policy": objective_cfg.selection_policy,
                "target_alerts_per_hour": list(objective_cfg.target_alerts_per_hour),
                "deployment_target_alerts_per_hour": float(objective_cfg.deployment_target_alerts_per_hour),
                "min_precision": float(objective_cfg.min_precision),
                "alert_band_scalar_score": (
                    val_band_meta.get("scalar_score") if isinstance(val_band_meta, dict) else None
                ),
                "val_pick_feasible": val_pick.feasible,
                "refit_train_plus_val": bool(cfg.refit_train_plus_val),
                "refit_best_iteration": refit_best_iteration,
                **policy_meta,
            },
            f,
            protocol=pickle.HIGHEST_PROTOCOL,
        )
    report["model_path"] = str(model_path)
    report["step5_model_path"] = str(model_path)
    metrics_path = (out_dir / DEFAULT_METRICS_FILENAME).resolve()
    report["training_metrics_path"] = str(metrics_path)
    report["step5_training_metrics_path"] = str(metrics_path)
    if persist_training_metrics:
        metrics_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    logger.info(
        "Step 5 trained LightGBM in %.3fs; threshold=%.6f feasible=%s val recall=%.4f prec=%.4f refit_train_plus_val=%s → %s",
        elapsed,
        thr,
        val_pick.feasible,
        val_pick.recall,
        val_pick.precision,
        bool(cfg.refit_train_plus_val),
        model_path,
    )

    return Step5Result(
        model_path=model_path,
        metrics_path=metrics_path,
        report=report,
        threshold=thr,
    )
