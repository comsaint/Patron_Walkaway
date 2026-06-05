"""Step 06: verify all model-feature train/serve parity for trained bundles.

Slow monthly rules follow ``Slow Feature Train-Serve Parity - IMPLEMENTATION_PLAN.md``:

- ``slow_anchor_target`` = last full calendar month before ``--as-of-date``.
- **Gap day** = first ``gaming_day`` epoch of that calendar month; training/serving read
  ``slow_anchor_effective`` (prior published month-end).
- **Post-gap** = second+ ``gaming_day`` epoch in the month; artifact must expose ``slow_anchor_target`` or the
  offline gate fails (mirrors entire-scorer hard stop).

Example (post-gap deploy gate):

    python trainer_hightier/06_verify_training_serving_parity.py \\
      --model-dir out/models_high_tier_mvp/20260522-124003-245bd1f \\
      --as-of-date 2026-05-22 \\
      --output-json out/models_high_tier_mvp/20260522-124003-245bd1f/feature_parity_verification.json

When ``--output-json`` is omitted and exactly one model bundle is verified, the report
defaults to ``<model-dir>/feature_parity_verification.json``.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd
import pyarrow.parquet as pq

from trainer_hightier.config import (
    DuckDbRuntimeConfig,
    HightierServingConfig,
    PRE_TRAIN_FEATURE_GATE_JSON_BASENAME,
    PreTrainFeatureGateConfig,
    SHORT_TERM_TRIAL_BET_COLUMNS,
    Step6ParityConfig,
    default_hightier_serving_config,
)
from trainer_hightier.feature_experiment.candidate_registry_loader import load_candidate_registry
from trainer_hightier.feature_experiment.feature_cadence import classify_model_fe_features
from trainer_hightier.feature_experiment.feast_mid_term_spike import SPIKE_MID_TERM_FEATURE_COLUMNS
from trainer_hightier.core.model_bundle_paths import (
    FEATURE_PARITY_REPORT_FILENAME,
    model_bundle_report_path,
)
from trainer_hightier.serving.adt_allowlist import resolve_model_bundle_allowlist_parquet
from trainer_hightier.serving.offline_serving_backtest import (
    _ScoringBatch,
    _bets_frame_from_test_batch,
    _build_feast_online_adapter,
    _iter_test_batches,
    build_pool_from_cleaned_parquet,
    resolve_offline_context,
    run_offline_production_pipeline,
)
from trainer_hightier.serving.model_bundle import load_hightier_model_bundle
from trainer_hightier.utils.slow_month_turn import (
    SlowMonthTurnPhase,
    gaming_day_epochs_in_calendar_month,
    resolve_slow_month_turn_context,
    slow_anchors_for_phase,
)

SLOW_FEATURE_COLUMNS = (
    "patron__theo_win_sum__w180d_m1snap",
    "patron__gaming_days_cnt__w180d_m1snap",
    "patron__adt__w180d_m1snap",
)
NUMERIC_TOLERANCE = 1e-6


def parse_gaming_day_dates(series: pd.Series) -> pd.Series:
    """Parse ``gaming_day`` values into ``datetime64[ns]`` for ordering."""
    parsed = pd.to_datetime(series, errors="coerce")
    if getattr(parsed.dt, "tz", None) is not None:
        parsed = parsed.dt.tz_convert(None)
    return parsed


def gaming_day_epochs_from_test_parquet(
    test_parquet: Path,
    *,
    year: int,
    month: int,
    max_rows: int,
) -> list[date]:
    """Sorted unique ``gaming_day`` dates in ``test_parquet`` for one calendar month."""
    cols = parquet_columns(test_parquet)
    if "gaming_day_event" not in cols:
        return []
    frame = read_parquet_sample(test_parquet, ["gaming_day_event"], max_rows=max_rows)
    ts = parse_gaming_day_dates(frame["gaming_day_event"]).dropna()
    epochs = [ts.loc[i].date() for i in ts.index]
    return gaming_day_epochs_in_calendar_month(epochs, year=year, month=month)


def resolve_slow_month_turn_phase(
    as_of_day: date,
    *,
    explicit: str | None,
    test_parquet: Path | None,
    max_rows: int,
) -> tuple[SlowMonthTurnPhase, dict[str, Any]]:
    """Resolve gap vs post-gap for ``--as-of-date`` (CLI override or test-sample inference)."""
    if explicit == "gap":
        return "gap", {"resolution": "cli", "explicit_phase": "gap"}
    if explicit == "post_gap":
        return "post_gap", {"resolution": "cli", "explicit_phase": "post_gap"}

    meta: dict[str, Any] = {"resolution": "default_post_gap"}
    if test_parquet is None:
        return "post_gap", meta

    epochs = gaming_day_epochs_from_test_parquet(
        test_parquet,
        year=as_of_day.year,
        month=as_of_day.month,
        max_rows=max_rows,
    )
    phase = resolve_slow_month_turn_context(as_of_day, month_epochs=epochs or None).phase
    meta["gaming_day_epochs_in_as_of_month"] = [d.isoformat() for d in epochs[:10]]
    if not epochs:
        meta["resolution"] = "default_post_gap_no_gaming_day_in_test_month"
        return "post_gap", meta
    first_epoch = epochs[0]
    second_epoch = epochs[1] if len(epochs) > 1 else None
    meta["first_gaming_day_epoch"] = first_epoch.isoformat()
    if second_epoch is not None:
        meta["second_gaming_day_epoch"] = second_epoch.isoformat()
    if as_of_day == first_epoch:
        return "gap", {**meta, "resolution": "inferred_gap_first_epoch"}
    if second_epoch is not None and as_of_day >= second_epoch:
        return "post_gap", {**meta, "resolution": "inferred_post_gap_second_plus_epoch"}
    if as_of_day > first_epoch:
        return "post_gap", {**meta, "resolution": "inferred_post_gap_after_first_epoch"}
    return "gap", {**meta, "resolution": "inferred_gap_before_second_epoch"}


def resolve_model_dirs(args: argparse.Namespace) -> list[Path]:
    """Resolve explicit model dirs, or scan one root for child dirs with ``model.pkl``."""
    explicit = [Path(p).resolve() for p in args.model_dir or ()]
    if explicit:
        return explicit
    root = Path(args.models_root).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"models root does not exist: {root}")
    return sorted(p for p in root.iterdir() if (p / "model.pkl").is_file())


def parquet_columns(path: Path) -> list[str]:
    """Read a Parquet file schema without scanning row data."""
    if not path.is_file():
        raise FileNotFoundError(f"parquet file not found: {path}")
    return list(pq.read_schema(path).names)


def read_parquet_sample(path: Path, columns: list[str], max_rows: int) -> pd.DataFrame:
    """Read selected columns from the first ``max_rows`` rows of a Parquet file."""
    pf = pq.ParquetFile(path)
    batches = []
    rows = 0
    for batch in pf.iter_batches(columns=columns, batch_size=min(max_rows, 50_000)):
        batches.append(batch)
        rows += batch.num_rows
        if rows >= max_rows:
            break
    if not batches:
        return pd.DataFrame(columns=columns)
    frame = pd.concat([b.to_pandas() for b in batches], ignore_index=True)
    return frame.head(max_rows).reset_index(drop=True)


def infer_anchor_column(columns: list[str]) -> str | None:
    """Infer the slow monthly anchor column from a Parquet schema."""
    by_lower = {c.lower(): c for c in columns}
    for name in ("anchor_gaming_day_event", "anchor_day", "snapshot_gaming_day"):
        if name in by_lower:
            return by_lower[name]
    return None


def load_model_feature_columns(model_dir: Path) -> tuple[str, ...]:
    """Load model feature columns from ``model.pkl``."""
    bundle = load_hightier_model_bundle(bundle_dir=model_dir)
    return bundle.feature_columns


def diff_mask(train: pd.Series, prod: pd.Series) -> pd.Series:
    """Return row-wise train/serve inequality for one aligned feature column."""
    train_na = train.isna()
    prod_na = prod.isna()
    out = train_na != prod_na
    both = ~(train_na | prod_na)
    if not bool(both.any()):
        return out
    train_both = train.loc[both]
    prod_both = prod.loc[both]
    train_num = pd.to_numeric(train_both, errors="coerce")
    prod_num = pd.to_numeric(prod_both, errors="coerce")
    numeric = ~(train_num.isna() | prod_num.isna())
    both_idx = train_both.index
    if bool(numeric.any()):
        out.loc[both_idx[numeric.to_numpy()]] = (
            (train_num.loc[numeric] - prod_num.loc[numeric]).abs() > NUMERIC_TOLERANCE
        ).to_numpy()
    if bool((~numeric).any()):
        non_num_idx = both_idx[(~numeric).to_numpy()]
        out.loc[non_num_idx] = (
            train_both.loc[non_num_idx].astype(str).to_numpy()
            != prod_both.loc[non_num_idx].astype(str).to_numpy()
        )
    return out


def _mid_term_parity_both_non_null_columns(feature_cols: list[str]) -> frozenset[str]:
    """Mid Feast columns: compare values only where train and serve are both non-null."""
    mid = frozenset(SPIKE_MID_TERM_FEATURE_COLUMNS)
    return frozenset(c for c in feature_cols if c in mid)


def short_term_parity_column_names(
    feature_cols: tuple[str, ...],
    *,
    registry_path: Path | None = None,
) -> tuple[str, ...]:
    """Resolve short-layer columns present in training split for Step 4.5 gate."""
    snap = load_candidate_registry(registry_path)
    fe_split = classify_model_fe_features(snap, feature_cols)
    cols = [
        *SHORT_TERM_TRIAL_BET_COLUMNS,
        *fe_split["short_term"],
    ]
    present = set(feature_cols)
    return tuple(dict.fromkeys(c for c in cols if c in present))


RAW_W1H_SANITY_COLUMN = "bet__bets_cnt__w1h"


def _raw_t_bet_partition_glob(raw_partition_dir: Path) -> str:
    """POSIX glob for monthly raw ``t_bet`` partition parquets."""
    root = Path(raw_partition_dir).resolve()
    return str((root / "t_bet__part_*.parquet").as_posix()).replace("'", "''")


def build_pool_from_raw_partitions(
    bets: pd.DataFrame,
    *,
    raw_partition_dir: Path,
    cfg: HightierServingConfig,
    mapping_parquet: Path,
) -> pd.DataFrame:
    """Bounded hot pool from raw monthly ``t_bet`` partitions (reference semantics)."""
    import duckdb

    from trainer_hightier.serving.offline_serving_backtest import resolve_hot_pool_player_ids
    from trainer_hightier.serving.scorer import compute_scoring_bounds_for_bets

    if bets.empty:
        return bets
    bounds = compute_scoring_bounds_for_bets(bets, cfg=cfg)
    if bounds.empty:
        raise ValueError("[raw_source_sanity] scoring bounds empty for non-empty bets batch")
    pool_start = bounds["pool_start"].min()
    pool_end = bounds["scoring_pcd"].max()
    if pd.isna(pool_start) or pd.isna(pool_end):
        raise ValueError("[raw_source_sanity] scoring bounds produced null pool window")
    pool_start = pd.Timestamp(pool_start).to_pydatetime()
    pool_end = pd.Timestamp(pool_end).to_pydatetime()
    pids = resolve_hot_pool_player_ids(bets, mapping_parquet, expand_canonical_aliases=False)
    fan_cap = int(cfg.hightier_scorer_pool_player_fanout_cap)
    if len(pids) > fan_cap:
        pids = pids[:fan_cap]
    glob_path = _raw_t_bet_partition_glob(raw_partition_dir)
    conn = duckdb.connect()
    try:
        conn.execute(
            "CREATE TEMP TABLE allow_pids AS SELECT * FROM (SELECT UNNEST(?) AS player_id)",
            [pids],
        )
        q = f"""
            SELECT
                TRY_CAST(CAST(b.bet_id AS VARCHAR) AS BIGINT) AS bet_id,
                TRY_CAST(CAST(b.is_back_bet AS VARCHAR) AS INTEGER) AS is_back_bet,
                CAST(b.bet_type AS VARCHAR) AS bet_type,
                CAST(b.type_of_bet AS VARCHAR) AS type_of_bet,
                CAST(b.payout_complete_dtm AS TIMESTAMPTZ) AS payout_complete_dtm,
                CAST(b.gaming_day AS DATE) AS gaming_day_event,
                TRY_CAST(CAST(b.session_id AS VARCHAR) AS DOUBLE) AS session_id,
                TRY_CAST(CAST(b.player_id AS VARCHAR) AS BIGINT) AS player_id,
                TRY_CAST(CAST(b.table_id AS VARCHAR) AS BIGINT) AS table_id,
                TRY_CAST(CAST(b.wager AS VARCHAR) AS DOUBLE) AS wager,
                TRY_CAST(CAST(b.casino_win AS VARCHAR) AS DOUBLE) AS casino_win,
                TRY_CAST(CAST(b.payout_odds AS VARCHAR) AS DOUBLE) AS payout_odds,
                TRY_CAST(CAST(b.theo_win AS VARCHAR) AS DOUBLE) AS theo_win,
                TRY_CAST(CAST(b.base_ha AS VARCHAR) AS DOUBLE) AS base_ha
            FROM read_parquet('{glob_path}') AS b
            INNER JOIN allow_pids AS p
              ON TRY_CAST(CAST(b.player_id AS VARCHAR) AS BIGINT) = p.player_id
            WHERE CAST(b.payout_complete_dtm AS TIMESTAMPTZ) >= ?
              AND CAST(b.payout_complete_dtm AS TIMESTAMPTZ) <= ?
              AND TRY_CAST(CAST(b.wager AS VARCHAR) AS DOUBLE) > 0
        """
        pool = conn.execute(q, [pool_start, pool_end]).fetchdf()
    finally:
        conn.close()
    if pool.empty:
        raise ValueError(
            "[raw_source_sanity] raw partition pool empty; check raw_partition_dir and dates",
        )
    pool = pool.drop_duplicates(subset=["bet_id"], keep="last")
    pool["__etl_insert_Dtm"] = pool["payout_complete_dtm"]
    from trainer_hightier.serving.feature_builder import attach_synthetic_etl_and_prediction_visible

    return attach_synthetic_etl_and_prediction_visible(pool)


def run_raw_source_w1h_sanity_check(
    test: pd.DataFrame,
    *,
    raw_partition_dir: Path,
    mapping_parquet: Path,
    max_rows: int = 200,
    undercount_ratio_threshold: float = 2.0,
    undercount_fail_fraction: float = 0.02,
    duckdb_runtime: DuckDbRuntimeConfig | None = None,
) -> dict[str, Any]:
    """Compare training ``bet__bets_cnt__w1h`` against raw ``t_bet`` partition recompute."""
    from trainer_hightier.serving.feature_builder import (
        attach_canonical_id,
        attach_synthetic_etl_and_prediction_visible,
        attach_trial_bet_behavior_1h,
    )
    from trainer_hightier.serving.short_term_scoring_context import sort_bets_for_scoring_batch

    issues: list[str] = []
    col = RAW_W1H_SANITY_COLUMN
    raw_dir = Path(raw_partition_dir).resolve()
    if not raw_dir.is_dir():
        return {
            "schema_version": "raw_source_w1h_sanity_v1",
            "verdict": "skipped",
            "issues": [f"raw partition dir missing: {raw_dir}"],
            "n_rows_compared": 0,
        }
    if col not in test.columns:
        return {
            "schema_version": "raw_source_w1h_sanity_v1",
            "verdict": "skipped",
            "issues": [f"training split missing column {col!r}"],
            "n_rows_compared": 0,
        }
    work = test.loc[test[col].notna(), ["bet_id", "player_id", "payout_complete_dtm", col]].copy()
    if work.empty:
        return {
            "schema_version": "raw_source_w1h_sanity_v1",
            "verdict": "skipped",
            "issues": [],
            "n_rows_compared": 0,
        }
    if max_rows and len(work) > int(max_rows):
        work = sort_bets_for_scoring_batch(work).head(int(max_rows)).reset_index(drop=True)
    need_cols = ("bet_id", "player_id", "payout_complete_dtm")
    missing = [c for c in need_cols if c not in work.columns]
    if missing:
        issues.append(f"sample missing required columns for raw recompute: {missing}")
        return {
            "schema_version": "raw_source_w1h_sanity_v1",
            "verdict": "fail",
            "issues": issues,
            "n_rows_compared": 0,
        }
    cfg = default_hightier_serving_config()
    runtime = duckdb_runtime or DuckDbRuntimeConfig()
    cmap = Path(mapping_parquet).resolve()
    if not cmap.is_file():
        issues.append(f"canonical mapping parquet missing: {cmap}")
        return {
            "schema_version": "raw_source_w1h_sanity_v1",
            "verdict": "fail",
            "issues": issues,
            "n_rows_compared": 0,
        }
    staged = sort_bets_for_scoring_batch(work)
    staged["__etl_insert_Dtm"] = pd.to_datetime(staged["payout_complete_dtm"], errors="coerce", utc=True)
    pool = build_pool_from_raw_partitions(
        staged,
        raw_partition_dir=raw_dir,
        cfg=cfg,
        mapping_parquet=cmap,
    )
    pool = attach_canonical_id(pool, mapping_parquet=cmap)
    staged = attach_synthetic_etl_and_prediction_visible(staged)
    staged = attach_canonical_id(staged, mapping_parquet=cmap)
    staged = attach_trial_bet_behavior_1h(staged, pool, duckdb_runtime=runtime)
    merged = work.merge(
        staged[["bet_id", col]].rename(columns={col: f"{col}_raw"}),
        on="bet_id",
        how="inner",
    )
    train_vals = pd.to_numeric(merged[col], errors="coerce")
    raw_vals = pd.to_numeric(merged[f"{col}_raw"], errors="coerce")
    eligible = train_vals.notna() & raw_vals.notna()
    n_eligible = int(eligible.sum())
    severe = (
        eligible
        & (raw_vals >= train_vals * float(undercount_ratio_threshold))
        & ((raw_vals - train_vals) >= 3)
    )
    n_severe = int(severe.sum())
    severe_fraction = float(n_severe / max(n_eligible, 1))
    examples = (
        merged.loc[severe, ["bet_id", col, f"{col}_raw"]]
        .head(5)
        .to_dict(orient="records")
        if n_severe
        else []
    )
    if n_eligible > 0 and severe_fraction > float(undercount_fail_fraction):
        issues.append(
            f"{n_severe}/{n_eligible} rows ({severe_fraction:.2%}) show severe training under-count "
            f"vs raw recompute (ratio>={undercount_ratio_threshold}, delta>=3); examples={examples}",
        )
    return {
        "schema_version": "raw_source_w1h_sanity_v1",
        "verdict": "fail" if issues else "pass",
        "issues": issues,
        "raw_partition_dir": str(raw_dir),
        "mapping_parquet": str(cmap),
        "column": col,
        "n_rows_input": int(len(work)),
        "n_rows_compared": n_eligible,
        "n_severe_undercount": n_severe,
        "severe_undercount_fraction": severe_fraction,
        "undercount_ratio_threshold": float(undercount_ratio_threshold),
        "undercount_fail_fraction": float(undercount_fail_fraction),
        "examples": examples,
    }


def pre_train_gate_exit_code(report: dict[str, Any]) -> int:
    """Non-zero when Step 4.5 short-term gate failed."""
    if report.get("verdict") == "fail":
        return 1
    return 0


def validate_slow_artifact(
    model_dir: Path,
    *,
    slow_anchor_target: date,
    slow_anchor_effective: date,
    month_turn_phase: SlowMonthTurnPhase,
    test_parquet: Path,
    max_rows: int,
) -> dict[str, Any]:
    """Validate deploy slow artifact schema and month-turn anchor contract."""
    slow_path = model_dir / "deploy_inputs" / "slow_patron_180d_monthly.parquet"
    required_anchor = slow_anchor_effective
    result: dict[str, Any] = {
        "path": str(slow_path),
        "slow_month_turn_phase": month_turn_phase,
        "slow_anchor_target": slow_anchor_target.isoformat(),
        "slow_anchor_effective": slow_anchor_effective.isoformat(),
        "slow_anchor_required_in_artifact": required_anchor.isoformat(),
        "issues": [],
    }
    try:
        cols = parquet_columns(slow_path)
    except FileNotFoundError as exc:
        result["issues"].append(str(exc))
        return result
    result["columns"] = cols
    anchor_col = infer_anchor_column(cols)
    result["anchor_column"] = anchor_col
    if "canonical_id" not in cols:
        result["issues"].append("slow artifact is not canonical-grain: missing canonical_id")
    if anchor_col is None:
        result["issues"].append("slow artifact is not anchor-grain: missing anchor_gaming_day_event")
    if "bet_id" in cols and ("canonical_id" not in cols or anchor_col is None):
        result["issues"].append("slow artifact appears bet-grain; it is not production-safe")
    if anchor_col is not None:
        anchor_report = validate_anchor_values(slow_path, anchor_col, required_anchor, month_turn_phase)
        result.update(anchor_report)
        result["issues"].extend(anchor_report.get("anchor_issues", []))
    if "canonical_id" in cols and anchor_col is not None:
        coverage_report = validate_slow_anchor_coverage(
            slow_path,
            anchor_col,
            slow_anchor_target=slow_anchor_target,
            required_anchor=required_anchor,
            month_turn_phase=month_turn_phase,
            test_parquet=test_parquet,
            max_rows=max_rows,
        )
        result.update(coverage_report)
        result["issues"].extend(coverage_report.get("coverage_issues", []))
    return result


def validate_anchor_values(
    path: Path,
    anchor_col: str,
    required_anchor: date,
    month_turn_phase: SlowMonthTurnPhase,
) -> dict[str, Any]:
    """Validate sampled slow parquet anchors match the required anchor for this month-turn phase."""
    frame = read_parquet_sample(path, [anchor_col], max_rows=1_000_000)
    anchors = pd.to_datetime(frame[anchor_col], errors="coerce").dt.date.dropna()
    distinct = sorted({x for x in anchors})
    distinct_iso = [str(x) for x in distinct]
    issues: list[str] = []
    if not distinct_iso:
        issues.append(f"anchor column {anchor_col!r} has no parseable dates")
        return {
            "distinct_anchors_sample": distinct_iso[:20],
            "n_distinct_anchors_sample": int(len(distinct_iso)),
            "anchor_issues": issues,
        }

    if distinct != [required_anchor]:
        issues.append(
            f"slow artifact must contain only slow_anchor_required={required_anchor.isoformat()} "
            f"for slow_month_turn_phase={month_turn_phase}, got {distinct_iso[:10]}",
        )
    unexpected = [x for x in distinct if x != required_anchor]
    if month_turn_phase == "post_gap" and unexpected:
        issues.append(
            "post-gap deploy gate forbids prior-month anchors in bundle artifact "
            "(entire scorer would hard-stop if slow_anchor_target is unavailable)",
        )

    return {
        "distinct_anchors_sample": distinct_iso[:20],
        "n_distinct_anchors_sample": int(len(distinct_iso)),
        "anchor_issues": issues,
    }


def validate_slow_anchor_coverage(
    slow_path: Path,
    anchor_col: str,
    *,
    slow_anchor_target: date,
    required_anchor: date,
    month_turn_phase: SlowMonthTurnPhase,
    test_parquet: Path,
    max_rows: int,
) -> dict[str, Any]:
    """Ensure every sampled patron has the required slow anchor row for this month-turn phase."""
    test_cols = parquet_columns(test_parquet)
    issues: list[str] = []
    if "canonical_id" not in test_cols:
        return {
            "coverage_issues": ["test split missing canonical_id; cannot validate per-patron slow anchor coverage"],
        }

    test_frame = read_parquet_sample(test_parquet, ["canonical_id"], max_rows=max_rows)
    canonical_ids = {
        str(x).strip()
        for x in test_frame["canonical_id"].dropna().tolist()
        if str(x).strip()
    }

    slow = read_parquet_sample(
        slow_path,
        ["canonical_id", anchor_col],
        max_rows=1_000_000,
    )
    slow["canonical_id"] = slow["canonical_id"].astype(str).str.strip()
    slow["_anchor"] = pd.to_datetime(slow[anchor_col], errors="coerce").dt.date
    covered = set(
        slow.loc[slow["_anchor"] == required_anchor, "canonical_id"].dropna().astype(str).tolist(),
    )
    missing = sorted(canonical_ids - covered)

    if missing:
        if month_turn_phase == "post_gap":
            issues.append(
                "post-gap: sampled canonical_ids lack slow_anchor_target rows; "
                "production would hard-stop entire scorer",
            )
        else:
            issues.append(
                "gap-day: sampled canonical_ids lack slow_anchor_effective (prior published) rows",
            )

    return {
        "n_sampled_training_canonical": int(len(canonical_ids)),
        "n_sampled_canonical_with_required_anchor": int(len(canonical_ids) - len(missing)),
        "n_sampled_canonical_missing_required_anchor": int(len(missing)),
        "sample_missing_required_anchor_canonical_ids": missing[:20],
        "coverage_issues": issues,
    }


def validate_training_split_static_slow(
    test_parquet: Path,
    *,
    model_features: tuple[str, ...],
    max_rows: int,
) -> dict[str, Any]:
    """Detect per-bet ASOF slow drift by checking per-canonical slow values are static."""
    slow_cols = [c for c in SLOW_FEATURE_COLUMNS if c in model_features]
    result: dict[str, Any] = {"slow_feature_columns": slow_cols, "issues": []}
    if not slow_cols:
        result["skipped"] = "model does not use slow feature columns"
        return result
    cols = parquet_columns(test_parquet)
    required = ["canonical_id", *slow_cols]
    missing = [c for c in required if c not in cols]
    if missing:
        result["issues"].append(f"test split missing required columns: {missing}")
        return result
    frame = read_parquet_sample(test_parquet, required, max_rows=max_rows)
    return result | summarize_static_slow_values(frame, slow_cols)


def _replay_feature_columns(
    model_features: tuple[str, ...],
    *,
    parity_cfg: Step6ParityConfig | None,
    registry_path: Path | None,
) -> tuple[list[str], frozenset[str] | None]:
    """Columns for Step 6 replay (optionally excludes short layer covered by Step 4.5)."""
    cols = list(model_features)
    mid_cols: frozenset[str] | None = None
    if registry_path is not None and registry_path.is_file():
        snap = load_candidate_registry(registry_path)
        fe_split = classify_model_fe_features(snap, model_features)
        mid_cols = frozenset(fe_split["mid_term"])
        if parity_cfg is not None and not parity_cfg.run_short_full_replay_in_step6:
            short_ids = set(fe_split["short_term"]) | set(SHORT_TERM_TRIAL_BET_COLUMNS)
            cols = [c for c in cols if c not in short_ids]
    return cols, mid_cols


def run_pre_train_feature_gate(
    test_parquet: Path,
    *,
    columns: tuple[str, ...],
    cleaned_bet_root: Path,
    mapping_parquet: Path,
    gate_cfg: PreTrainFeatureGateConfig,
    duckdb_runtime: DuckDbRuntimeConfig | None = None,
    output_json: Path | None = None,
    raw_partition_dir: Path | None = None,
) -> dict[str, Any]:
    """Step 4.5: compare training short-layer columns to live bounded PIT replay."""
    if not gate_cfg.run_pre_train_gate:
        report = {"schema_version": "pre_train_feature_gate_v1", "verdict": "skipped", "issues": []}
        if output_json is not None:
            out = Path(output_json).resolve()
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        return report
    from trainer_hightier.config import DuckDbRuntimeConfig
    from trainer_hightier.serving.short_term_scoring_context import (
        build_short_term_features_for_batch,
        default_short_term_scoring_context,
        split_short_term_column_names,
    )

    issues: list[str] = []
    if not columns:
        return {
            "schema_version": "pre_train_feature_gate_v1",
            "verdict": "pass",
            "issues": [],
            "n_rows_compared": 0,
            "columns": [],
        }
    if not test_parquet.is_file():
        issues.append(f"test parquet missing: {test_parquet}")
        return {
            "schema_version": "pre_train_feature_gate_v1",
            "verdict": "fail",
            "issues": issues,
            "n_rows_compared": 0,
            "columns": list(columns),
        }
    test = pd.read_parquet(test_parquet)
    missing = [c for c in columns if c not in test.columns]
    if missing:
        issues.append(f"test split missing gate columns: {missing}")
        return {
            "schema_version": "pre_train_feature_gate_v1",
            "verdict": "fail",
            "issues": issues,
            "n_rows_compared": 0,
            "columns": list(columns),
        }
    if gate_cfg.max_rows and len(test) > gate_cfg.max_rows:
        from trainer_hightier.serving.short_term_scoring_context import sort_bets_for_scoring_batch

        test = sort_bets_for_scoring_batch(test).head(int(gate_cfg.max_rows)).reset_index(drop=True)
    trial_cols, fe_cols = split_short_term_column_names(columns)
    serving_cfg = default_hightier_serving_config()
    ctx = default_short_term_scoring_context(serving_cfg)
    runtime = duckdb_runtime or DuckDbRuntimeConfig()
    prod_parts: list[pd.DataFrame] = []
    for batch_df in _iter_test_batches(test, batch_size=gate_cfg.batch_size, max_rows=None):
        bets = _bets_frame_from_test_batch(batch_df)
        prod_parts.append(
            build_short_term_features_for_batch(
                bets,
                cleaned_bet_parquet=cleaned_bet_root,
                mapping_parquet=mapping_parquet,
                serving_cfg=serving_cfg,
                duckdb_runtime=runtime,
                fe_columns=fe_cols,
                trial_columns=trial_cols,
                context=ctx,
            ),
        )
    prod = pd.concat(prod_parts, ignore_index=True) if prod_parts else pd.DataFrame()
    train = test[["bet_id", *columns]].copy()
    merged = train.merge(prod, on="bet_id", suffixes=("_train", "_serve"), how="inner")
    per_feature = summarize_feature_diffs(merged, list(columns))
    fail_features = [
        r
        for r in per_feature
        if r["n_diff"] > 0 and float(r["diff_fraction"]) > float(gate_cfg.diff_fraction_fail_threshold)
    ]
    if fail_features:
        issues.append(
            f"{len(fail_features)} short column(s) exceed diff fraction "
            f"{gate_cfg.diff_fraction_fail_threshold}",
        )
    raw_sanity: dict[str, Any] = {"verdict": "skipped", "issues": []}
    if raw_partition_dir is not None and RAW_W1H_SANITY_COLUMN in columns:
        raw_sanity = run_raw_source_w1h_sanity_check(
            test,
            raw_partition_dir=Path(raw_partition_dir),
            mapping_parquet=mapping_parquet,
            max_rows=min(int(gate_cfg.max_rows), 200),
            duckdb_runtime=runtime,
        )
        if raw_sanity.get("verdict") == "fail":
            issues.extend(raw_sanity.get("issues", []))
    report = {
        "schema_version": "pre_train_feature_gate_v1",
        "verdict": "fail" if issues else "pass",
        "issues": issues,
        "test_parquet": str(test_parquet.resolve()),
        "cleaned_bet_root": str(cleaned_bet_root.resolve()),
        "n_rows_input": int(len(test)),
        "n_rows_compared": int(len(merged)),
        "columns": list(columns),
        "expand_canonical_aliases": False,
        "batch_size": int(gate_cfg.batch_size),
        "features_all": per_feature,
        "features_with_diff": [r for r in per_feature if r["n_diff"] > 0],
        "raw_source_w1h_sanity": raw_sanity,
    }
    if output_json is not None:
        out = Path(output_json).resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return report


def default_pre_train_gate_json_path() -> Path:
    """Default Step 4.5 report path under training_data artifacts."""
    root = Path(__file__).resolve().parent
    return (root / "artifacts" / "training_data" / PRE_TRAIN_FEATURE_GATE_JSON_BASENAME).resolve()


def run_production_feature_replay(
    model_dir: Path,
    test_parquet: Path,
    *,
    cleaned_bet_root: Path,
    feast_repo: Path,
    max_rows: int,
    batch_size: int,
    diff_fraction_fail_threshold: float = 0.02,
    parity_cfg: Step6ParityConfig | None = None,
) -> dict[str, Any]:
    """Replay production suppliers and compare every model feature to training split values.

    Mid-term ``fe__*`` uses the Step 3.5 training snapshot ASOF join (not Feast latest-anchor
    lookup) so historical test bets match training enrich; slow ``patron__*`` still uses Feast online.
    """
    ctx = resolve_offline_context(
        bundle_dir=None,
        model_dir=model_dir,
        mapping_parquet=model_dir / "deploy_inputs" / "canonical_player_mapping.parquet",
        allowlist_parquet=resolve_model_bundle_allowlist_parquet(model_dir),
        feast_repo=feast_repo,
        slow_patron_parquet=None,
        use_feast_online=True,
        use_training_mid_snapshot_for_parity=True,
    )
    needs_feast = bool(ctx.supplier_plan.feast_mid_cols or ctx.supplier_plan.feast_slow_cols)
    adapter = _build_feast_online_adapter(ctx) if needs_feast else None
    registry_path = model_dir / "feature_candidate_registry.snapshot.yaml"
    replay_cols, mid_cols = _replay_feature_columns(
        ctx.bundle.feature_columns,
        parity_cfg=parity_cfg,
        registry_path=registry_path,
    )
    test = pd.read_parquet(test_parquet)
    if max_rows and len(test) > max_rows:
        from trainer_hightier.serving.short_term_scoring_context import sort_bets_for_scoring_batch

        test = sort_bets_for_scoring_batch(test).head(int(max_rows)).reset_index(drop=True)
    if not replay_cols:
        return {
            "mode": "all_model_features",
            "issues": [],
            "n_rows_compared": 0,
            "feature_count": 0,
            "skipped_short_full_replay": True,
        }
    return compare_training_to_production_features(
        test,
        ctx=ctx,
        adapter=adapter,
        cleaned_bet_root=cleaned_bet_root,
        batch_size=batch_size,
        diff_fraction_fail_threshold=diff_fraction_fail_threshold,
        feature_cols=replay_cols,
        mid_columns=mid_cols,
    )


def compare_training_to_production_features(
    test: pd.DataFrame,
    *,
    ctx: Any,
    adapter: Any,
    cleaned_bet_root: Path,
    batch_size: int,
    diff_fraction_fail_threshold: float = 0.02,
    feature_cols: list[str] | None = None,
    mid_columns: frozenset[str] | None = None,
) -> dict[str, Any]:
    """Compare production-replayed model feature values against training parquet columns."""
    feature_cols = list(feature_cols if feature_cols is not None else ctx.bundle.feature_columns)
    missing_train = [c for c in feature_cols if c not in test.columns]
    issues = []
    if missing_train:
        issues.append(f"training split missing model feature columns: {missing_train}")
        return {
            "mode": "all_model_features",
            "issues": issues,
            "n_rows_compared": 0,
            "feature_count": len(feature_cols),
        }
    prod_parts: list[pd.DataFrame] = []
    skipped_entity_missing = 0
    feast_entity_missing = 0
    feast_cell_null: dict[str, int] = {}
    smoke_failures: list[str] = []
    cfg = ctx.cfg
    for batch_df in _iter_test_batches(test, batch_size=batch_size, max_rows=None):
        prod_parts.append(
            run_feature_replay_batch(
                batch_df,
                ctx=ctx,
                adapter=adapter,
                cfg=cfg,
                cleaned_bet_root=cleaned_bet_root,
            ),
        )
        batch_diag = prod_parts[-1].attrs.get("diagnostics", {})
        skipped_entity_missing += int(batch_diag.get("skipped_entity_missing", 0))
        feast_entity_missing += int(batch_diag.get("feast_entity_missing", 0))
        for col, count in dict(batch_diag.get("feast_cell_null_counts", {})).items():
            feast_cell_null[col] = feast_cell_null.get(col, 0) + int(count)
        smoke_failures.extend(batch_diag.get("smoke_failures", []))
    prod = pd.concat(prod_parts, ignore_index=True) if prod_parts else pd.DataFrame()
    train = test[["bet_id", *feature_cols]].copy()
    merged = train.merge(prod, on="bet_id", suffixes=("_train", "_serve"), how="inner")
    per_feature = summarize_feature_diffs(merged, feature_cols, mid_columns=mid_columns)
    n_changed = sum(1 for r in per_feature if r["n_diff"] > 0)
    fail_features = [
        r
        for r in per_feature
        if r["n_diff"] > 0 and float(r["diff_fraction"]) > float(diff_fraction_fail_threshold)
    ]
    if fail_features:
        issues.append(
            f"{len(fail_features)} model feature column(s) exceed train/serve diff fraction "
            f"{diff_fraction_fail_threshold} (of {n_changed} with any diff)",
        )
    if skipped_entity_missing:
        issues.append(f"production replay skipped {skipped_entity_missing} rows due to Feast entity missing")
    if smoke_failures:
        issues.append(f"post-join smoke failures: {sorted(set(smoke_failures))}")
    return {
        "mode": "all_model_features",
        "issues": issues,
        "n_rows_input": int(len(test)),
        "n_rows_compared": int(len(merged)),
        "feature_count": len(feature_cols),
        "n_features_with_diff": int(n_changed),
        "max_feature_diff_fraction": max((r["diff_fraction"] for r in per_feature), default=0.0),
        "features_with_diff": [r for r in per_feature if r["n_diff"] > 0],
        "features_all": per_feature,
        "skipped_entity_missing": int(skipped_entity_missing),
        "feast_entity_missing": int(feast_entity_missing),
        "feast_cell_null_counts": feast_cell_null,
        "smoke_failures": sorted(set(smoke_failures)),
    }


def run_feature_replay_batch(
    batch_df: pd.DataFrame,
    *,
    ctx: Any,
    adapter: Any,
    cfg: HightierServingConfig,
    cleaned_bet_root: Path,
) -> pd.DataFrame:
    """Replay production feature suppliers for one test batch."""
    bets = _bets_frame_from_test_batch(batch_df)
    pool = build_pool_from_cleaned_parquet(
        bets,
        cleaned_root=cleaned_bet_root,
        cfg=cfg,
        mapping_parquet=ctx.mapping_parquet,
        expand_canonical_aliases=False,
    )
    scoring_batch = _ScoringBatch(
        bets=bets.reset_index(drop=True),
        cursor=pd.to_datetime(bets["__etl_insert_Dtm"], errors="coerce"),
        pool=pool,
    )
    result = run_offline_production_pipeline(
        scoring_batch,
        ctx,
        adapter,
        strict_smoke=False,
        allow_slow_parquet_fallback=False,
    )
    feature_cols = list(ctx.bundle.feature_columns)
    out = result.staged[["bet_id", *feature_cols]].copy()
    out.attrs["diagnostics"] = {
        "skipped_entity_missing": int(len(result.skipped_entity_missing)),
        "feast_entity_missing": int(result.feast_diag.n_entity_missing),
        "feast_cell_null_counts": dict(result.feast_diag.cell_null_counts or {}),
        "smoke_failures": list(result.smoke_failures),
    }
    return out


def summarize_feature_diffs(
    merged: pd.DataFrame,
    feature_cols: list[str],
    *,
    compare_only_when_train_present: bool = False,
    mid_columns: frozenset[str] | None = None,
) -> list[dict[str, Any]]:
    """Build per-feature train/serve diff summaries.

    When ``compare_only_when_train_present`` is True (mid structural nulls), rows with
    null training values are excluded from the diff denominator.
    """
    rows = []
    denom = max(len(merged), 1)
    for col in feature_cols:
        train_col = f"{col}_train"
        serve_col = f"{col}_serve"
        if train_col not in merged.columns or serve_col not in merged.columns:
            rows.append({"feature": col, "n_diff": denom, "diff_fraction": 1.0, "issue": "missing comparison column"})
            continue
        train_s = merged[train_col]
        serve_s = merged[serve_col]
        use_train_present_only = compare_only_when_train_present or (
            mid_columns is not None and col in mid_columns
        )
        if use_train_present_only:
            eligible = train_s.notna()
            n_eligible = int(eligible.sum())
            if n_eligible == 0:
                rows.append(
                    {
                        "feature": col,
                        "n_diff": 0,
                        "diff_fraction": 0.0,
                        "train_null_fraction": float(train_s.isna().mean()) if len(merged) else 0.0,
                        "serve_null_fraction": float(serve_s.isna().mean()) if len(merged) else 0.0,
                        "n_rows_compared": 0,
                    },
                )
                continue
            mask = diff_mask(train_s.loc[eligible], serve_s.loc[eligible])
            rows.append(
                {
                    "feature": col,
                    "n_diff": int(mask.sum()),
                    "diff_fraction": float(mask.mean()) if len(mask) else 0.0,
                    "train_null_fraction": float(train_s.isna().mean()) if len(merged) else 0.0,
                    "serve_null_fraction": float(serve_s.isna().mean()) if len(merged) else 0.0,
                    "n_rows_compared": n_eligible,
                },
            )
            continue
        mask = diff_mask(train_s, serve_s)
        rows.append(
            {
                "feature": col,
                "n_diff": int(mask.sum()),
                "diff_fraction": float(mask.mean()) if len(mask) else 0.0,
                "train_null_fraction": float(merged[train_col].isna().mean()) if len(merged) else 0.0,
                "serve_null_fraction": float(merged[serve_col].isna().mean()) if len(merged) else 0.0,
            },
        )
    return sorted(rows, key=lambda r: (-float(r["diff_fraction"]), str(r["feature"])))


def summarize_static_slow_values(frame: pd.DataFrame, slow_cols: list[str]) -> dict[str, Any]:
    """Summarize whether each canonical has one slow-feature tuple in sampled rows."""
    if frame.empty:
        return {"n_rows": 0, "issues": ["test split sample is empty"]}
    work = frame[["canonical_id", *slow_cols]].copy()
    work["canonical_id"] = work["canonical_id"].astype(str).str.strip()
    for col in slow_cols:
        work[col] = work[col].astype("string").fillna("<NA>")
    combos = work.drop_duplicates(["canonical_id", *slow_cols])
    counts = combos.groupby("canonical_id", dropna=False).size()
    multi = counts[counts > 1]
    issues = []
    if len(multi):
        issues.append(
            "training split slow features vary within canonical_id; "
            "this indicates per-bet/month ASOF rather than fixed last-full-month values",
        )
    return {
        "n_rows": int(len(work)),
        "n_canonical": int(counts.size),
        "n_canonical_with_multiple_slow_tuples": int(len(multi)),
        "pct_canonical_with_multiple_slow_tuples": float(len(multi) / max(counts.size, 1)),
        "max_slow_tuples_per_canonical": int(counts.max()) if len(counts) else 0,
        "issues": issues,
    }


def validate_one_model(
    model_dir: Path,
    test_parquet: Path,
    *,
    slow_anchor_target: date,
    slow_anchor_effective: date,
    month_turn_phase: SlowMonthTurnPhase,
    cleaned_bet_root: Path,
    feast_repo: Path,
    max_rows: int,
    batch_size: int,
    diff_fraction_fail_threshold: float = 0.02,
    parity_cfg: Step6ParityConfig | None = None,
    raw_partition_dir: Path | None = None,
    mapping_parquet: Path | None = None,
) -> dict[str, Any]:
    """Run all-feature parity checks for one trained model directory."""
    report: dict[str, Any] = {
        "model_dir": str(model_dir),
        "slow_month_turn_phase": month_turn_phase,
        "slow_anchor_target": slow_anchor_target.isoformat(),
        "slow_anchor_effective": slow_anchor_effective.isoformat(),
        "issues": [],
    }
    try:
        features = load_model_feature_columns(model_dir)
    except (FileNotFoundError, ValueError, OSError) as exc:
        report["issues"].append(f"failed to load model bundle: {exc}")
        report["verdict"] = "fail"
        return report
    slow_features = [c for c in SLOW_FEATURE_COLUMNS if c in features]
    report["slow_features_in_model"] = slow_features
    report["slow_artifact"] = validate_slow_artifact(
        model_dir,
        slow_anchor_target=slow_anchor_target,
        slow_anchor_effective=slow_anchor_effective,
        month_turn_phase=month_turn_phase,
        test_parquet=test_parquet,
        max_rows=max_rows,
    )
    report["training_split_static_slow"] = validate_training_split_static_slow(
        test_parquet,
        model_features=features,
        max_rows=max_rows,
    )
    try:
        report["all_feature_replay"] = run_production_feature_replay(
            model_dir,
            test_parquet,
            cleaned_bet_root=cleaned_bet_root,
            feast_repo=feast_repo,
            max_rows=max_rows,
            batch_size=batch_size,
            diff_fraction_fail_threshold=diff_fraction_fail_threshold,
            parity_cfg=parity_cfg,
        )
    except (FileNotFoundError, ValueError, OSError, RuntimeError) as exc:
        report["all_feature_replay"] = {
            "mode": "all_model_features",
            "issues": [f"production feature replay failed: {exc}"],
        }
    cfg6 = parity_cfg or Step6ParityConfig()
    raw_sanity: dict[str, Any] = {"verdict": "skipped", "issues": []}
    if cfg6.run_raw_source_sanity and raw_partition_dir is not None:
        from trainer_hightier.utils.canonical_mapping import default_canonical_mapping_parquet_path

        cmap = (
            Path(mapping_parquet).resolve()
            if mapping_parquet is not None
            else default_canonical_mapping_parquet_path().resolve()
        )
        try:
            test_frame = pd.read_parquet(test_parquet)
            if max_rows and len(test_frame) > max_rows:
                from trainer_hightier.serving.short_term_scoring_context import sort_bets_for_scoring_batch

                test_frame = sort_bets_for_scoring_batch(test_frame).head(int(max_rows)).reset_index(drop=True)
            raw_sanity = run_raw_source_w1h_sanity_check(
                test_frame,
                raw_partition_dir=Path(raw_partition_dir),
                mapping_parquet=cmap,
                max_rows=int(cfg6.raw_source_sanity_max_rows),
                undercount_ratio_threshold=float(cfg6.raw_source_undercount_ratio_threshold),
                undercount_fail_fraction=float(cfg6.raw_source_undercount_fail_fraction),
            )
        except (FileNotFoundError, ValueError, OSError, RuntimeError) as exc:
            raw_sanity = {
                "schema_version": "raw_source_w1h_sanity_v1",
                "verdict": "fail",
                "issues": [f"raw source w1h sanity failed: {exc}"],
                "n_rows_compared": 0,
            }
    report["raw_source_w1h_sanity"] = raw_sanity
    if raw_sanity.get("verdict") == "fail":
        report["issues"].extend(raw_sanity.get("issues", []))
    report["issues"].extend(report["slow_artifact"].get("issues", []))
    report["issues"].extend(report["training_split_static_slow"].get("issues", []))
    report["issues"].extend(report["all_feature_replay"].get("issues", []))

    n_model_features = len(features)
    replay = report.get("all_feature_replay") or {}
    n_compared = int(replay.get("feature_count") or 0)
    if n_model_features > 0 and n_compared < n_model_features:
        report["issues"].append(
            f"all_feature_replay compared {n_compared} of {n_model_features} model features; "
            "enable run_short_full_replay_in_step6 or fix replay coverage",
        )

    slow_issues = (
        report["slow_artifact"].get("issues", [])
        + report["training_split_static_slow"].get("issues", [])
    )
    all_feature_issues = list(report["all_feature_replay"].get("issues", []))
    report["slow_gate"] = {
        "verdict": "fail" if slow_issues else "pass",
        "issues": slow_issues,
    }
    report["all_feature_gate"] = {
        "verdict": "fail" if all_feature_issues else "pass",
        "issues": all_feature_issues,
    }
    raw_issues = list(raw_sanity.get("issues", [])) if raw_sanity.get("verdict") == "fail" else []
    report["raw_source_gate"] = {
        "verdict": "fail" if raw_issues else "pass",
        "issues": raw_issues,
    }
    report["verdict"] = "fail" if (slow_issues or all_feature_issues or raw_issues) else "pass"
    return report


def model_exit_code(
    report: dict[str, Any],
    *,
    parity_cfg: Step6ParityConfig,
) -> int:
    """Return non-zero when configured parity gates fail."""
    slow_fail = report.get("slow_gate", {}).get("verdict") == "fail"
    all_fail = report.get("all_feature_gate", {}).get("verdict") == "fail"
    if parity_cfg.hard_fail_slow_gate and slow_fail:
        return 1
    if parity_cfg.hard_fail_all_feature_gate and all_fail:
        return 1
    raw_fail = report.get("raw_source_gate", {}).get("verdict") == "fail"
    if parity_cfg.hard_fail_raw_source_sanity and raw_fail:
        return 1
    return 0


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    """Build the multi-model feature parity verification report."""
    as_of = date.fromisoformat(str(args.as_of_date))
    explicit_phase = None if str(args.month_turn_phase) == "auto" else str(args.month_turn_phase)
    test_parquet = Path(args.test_parquet).resolve()
    phase, phase_meta = resolve_slow_month_turn_phase(
        as_of,
        explicit=explicit_phase,
        test_parquet=test_parquet,
        max_rows=int(args.max_rows),
    )
    slow_anchor_target, slow_anchor_effective = slow_anchors_for_phase(as_of, phase)
    model_dirs = resolve_model_dirs(args)
    cleaned_bet_root = Path(args.cleaned_bet).resolve()
    feast_repo = Path(args.feast_repo).resolve()
    raw_partition_dir = (
        Path(args.raw_partition_dir).resolve()
        if getattr(args, "raw_partition_dir", None) is not None
        else None
    )
    mapping_parquet = (
        Path(args.canonical_mapping).resolve()
        if getattr(args, "canonical_mapping", None) is not None
        else None
    )
    models = [
        validate_one_model(
            model_dir,
            test_parquet,
            slow_anchor_target=slow_anchor_target,
            slow_anchor_effective=slow_anchor_effective,
            month_turn_phase=phase,
            cleaned_bet_root=cleaned_bet_root,
            feast_repo=feast_repo,
            max_rows=int(args.max_rows),
            batch_size=int(args.batch_size),
            diff_fraction_fail_threshold=float(
                getattr(args, "all_feature_diff_fraction_fail_threshold", 0.02),
            ),
            parity_cfg=getattr(args, "parity_cfg", None),
            raw_partition_dir=raw_partition_dir,
            mapping_parquet=mapping_parquet,
        )
        for model_dir in model_dirs
    ]
    return {
        "schema_version": "feature_parity_verification_v2",
        "as_of_date": as_of.isoformat(),
        "slow_month_turn_phase": phase,
        "slow_month_turn_phase_resolution": phase_meta,
        "slow_anchor_target": slow_anchor_target.isoformat(),
        "slow_anchor_effective": slow_anchor_effective.isoformat(),
        "test_parquet": str(test_parquet),
        "cleaned_bet_root": str(cleaned_bet_root),
        "raw_partition_dir": str(raw_partition_dir) if raw_partition_dir is not None else None,
        "feast_repo": str(feast_repo),
        "n_models": len(models),
        "n_failed": sum(1 for m in models if m.get("verdict") != "pass"),
        "n_failed_slow_gate": sum(1 for m in models if m.get("slow_gate", {}).get("verdict") == "fail"),
        "n_failed_all_feature_gate": sum(
            1 for m in models if m.get("all_feature_gate", {}).get("verdict") == "fail"
        ),
        "parity_gate": {
            "hard_fail_slow_gate": bool(getattr(args, "hard_fail_slow_gate", True)),
            "hard_fail_all_feature_gate": bool(getattr(args, "hard_fail_all_feature_gate", False)),
            "all_feature_diff_fraction_fail_threshold": float(
                getattr(args, "all_feature_diff_fraction_fail_threshold", 0.02),
            ),
        },
        "models": models,
    }


def build_report_from_config(
    *,
    model_dirs: list[Path],
    test_parquet: Path,
    cleaned_bet_root: Path,
    feast_repo: Path,
    as_of_date: date,
    parity_cfg: Step6ParityConfig,
    month_turn_phase: str = "auto",
) -> dict[str, Any]:
    """Programmatic entry for trainer Step 6 (same report as CLI)."""
    class _Args:
        pass

    args = _Args()
    args.model_dir = model_dirs
    args.models_root = Path("out/models_high_tier_mvp")
    args.test_parquet = test_parquet
    args.cleaned_bet = cleaned_bet_root
    args.feast_repo = feast_repo
    args.as_of_date = as_of_date.isoformat()
    args.month_turn_phase = month_turn_phase
    args.max_rows = parity_cfg.max_rows
    args.batch_size = parity_cfg.batch_size
    args.hard_fail_slow_gate = parity_cfg.hard_fail_slow_gate
    args.hard_fail_all_feature_gate = parity_cfg.hard_fail_all_feature_gate
    args.all_feature_diff_fraction_fail_threshold = (
        parity_cfg.all_feature_diff_fraction_fail_threshold
    )
    args.parity_cfg = parity_cfg
    from trainer_hightier.utils.partition_inventory import default_partition_snapshot_dir

    args.raw_partition_dir = default_partition_snapshot_dir()
    args.canonical_mapping = None
    return build_report(args)


def resolve_output_json_path(args: argparse.Namespace, model_dirs: list[Path]) -> Path:
    """Resolve CLI ``--output-json`` or default beside a single model bundle."""
    if args.output_json is not None:
        return Path(args.output_json).resolve()
    if len(model_dirs) != 1:
        raise ValueError(
            "--output-json is required when verifying multiple model bundles; "
            f"got {len(model_dirs)} model dirs",
        )
    return model_bundle_report_path(model_dirs[0], FEATURE_PARITY_REPORT_FILENAME)


def report_exit_code(report: dict[str, Any], *, parity_cfg: Step6ParityConfig) -> int:
    """Aggregate exit code across models for configured gates."""
    for model in report.get("models", []):
        if model_exit_code(model, parity_cfg=parity_cfg) != 0:
            return 1
    return 0


def run_cli(argv: list[str] | None = None) -> int:
    """CLI entrypoint for Step 06 all-feature parity verification."""
    parser = argparse.ArgumentParser(description="Verify all model feature train/serve parity")
    parser.add_argument("--model-dir", type=Path, action="append", default=None)
    parser.add_argument("--models-root", type=Path, default=Path("out/models_high_tier_mvp"))
    parser.add_argument("--test-parquet", type=Path, default=Path("trainer_hightier/artifacts/training_data/splits/test.parquet"))
    parser.add_argument("--cleaned-bet", type=Path, default=Path("trainer_hightier/artifacts/cleaned/cleaned__gmwds_t_bet"))
    parser.add_argument(
        "--raw-partition-dir",
        type=Path,
        default=Path("data/partitions"),
        help="raw monthly t_bet partition dir for raw-source w1h sanity check",
    )
    parser.add_argument(
        "--canonical-mapping",
        type=Path,
        default=None,
        help="canonical mapping parquet for raw-source sanity (default bundled artifact)",
    )
    parser.add_argument("--feast-repo", type=Path, default=Path("trainer_hightier/feast_repo"))
    parser.add_argument("--as-of-date", type=str, default=date.today().isoformat())
    parser.add_argument(
        "--month-turn-phase",
        type=str,
        default="auto",
        choices=("auto", "gap", "post_gap"),
        help=(
            "Month-turn phase for slow anchor gates: auto infers from test gaming_day epochs in "
            "--as-of-date month (defaults to post_gap if unknown); gap=first gaming_day epoch uses "
            "prior published anchor; post_gap=second+ epoch requires slow_anchor_target in artifact"
        ),
    )
    parser.add_argument("--max-rows", type=int, default=200_000)
    parser.add_argument("--batch-size", type=int, default=2000)
    parser.add_argument(
        "--hard-fail-slow-gate",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="exit non-zero when slow_gate fails (default: true)",
    )
    parser.add_argument(
        "--hard-fail-all-feature-gate",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="exit non-zero when all_feature_gate fails (default: false, monitoring only)",
    )
    parser.add_argument(
        "--output-json",
        type=Path,
        default=None,
        help=(
            "write JSON report path (default: <single-model-dir>/"
            f"{FEATURE_PARITY_REPORT_FILENAME})"
        ),
    )
    parser.add_argument("--no-fail", action="store_true", help="always exit 0 after writing JSON")
    args = parser.parse_args(argv)
    parity_cfg = Step6ParityConfig(
        hard_fail_slow_gate=bool(args.hard_fail_slow_gate),
        hard_fail_all_feature_gate=bool(args.hard_fail_all_feature_gate),
        max_rows=int(args.max_rows),
        batch_size=int(args.batch_size),
        run_short_full_replay_in_step6=bool(
            getattr(args, "run_short_full_replay_in_step6", False),
        ),
    )
    model_dirs = resolve_model_dirs(args)
    try:
        out = resolve_output_json_path(args, model_dirs)
    except ValueError as exc:
        parser.error(str(exc))
    args.parity_cfg = parity_cfg
    report = build_report(args)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"wrote {out}")
    print(
        f"models={report['n_models']} failed={report['n_failed']} "
        f"slow_gate_failed={report['n_failed_slow_gate']} "
        f"all_feature_failed={report['n_failed_all_feature_gate']} "
        f"phase={report['slow_month_turn_phase']} "
        f"target={report['slow_anchor_target']} effective={report['slow_anchor_effective']}",
    )
    if args.no_fail:
        return 0
    return report_exit_code(report, parity_cfg=parity_cfg)


if __name__ == "__main__":
    raise SystemExit(run_cli())
