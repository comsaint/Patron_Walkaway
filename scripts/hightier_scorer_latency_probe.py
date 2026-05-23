"""Run one high-tier scorer latency probe directly from a model folder.

This script is intentionally outside the deploy bundle path. It exercises the same
serving stages as the scorer, but reports per-stage timing so production slowness
can be isolated without rebuilding a bundle.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from trainer_hightier.config import (
    HK_TZ,
    default_hightier_serving_config,
    set_hightier_serving_deploy_override,
)
from trainer_hightier.feature_experiment.feature_cadence import runtime_inputs_from_registry
from trainer_hightier.serving.adt_allowlist import load_adt_allowlist_ids
from trainer_hightier.serving.feast_online_adapter import (
    FeastSdkOnlineAdapter,
    compute_row_missing_audits,
    enrich_row_audits_composite_upstream,
)
from trainer_hightier.serving.feature_builder import (
    assert_features_ready,
    attach_mid_term_composite_columns,
    coerce_categoricals,
)
from trainer_hightier.serving.feature_supply import (
    assert_scorer_supplier_plan_or_raise,
    build_scorer_supplier_plan,
    load_frozen_registry_for_bundle,
)
from trainer_hightier.serving.model_bundle import load_hightier_model_bundle
from trainer_hightier.serving.prediction_log import append_hightier_prediction_log, init_prediction_log_db
from trainer_hightier.serving.scorer import (
    _attach_feast_mid_slow,
    _build_staged_features,
    _fetch_scoring_batch,
)
from trainer_hightier.serving.state_db import connect_state_db, init_state_db

LOGGER = logging.getLogger("hightier_scorer_latency_probe")


def _parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(description="Probe high-tier scorer latency without bundling.")
    parser.add_argument("--model-dir", type=Path, required=True, help="Folder containing model.pkl.")
    parser.add_argument("--mapping", type=Path, default=None, help="canonical_player_mapping.parquet.")
    parser.add_argument("--allowlist", type=Path, default=None, help="adt_allowed_players parquet.")
    parser.add_argument("--feast-repo", type=Path, default=Path("trainer_hightier/feast_repo"))
    parser.add_argument("--state-db", type=Path, default=Path(".data/hightier_latency_probe/state.db"))
    parser.add_argument("--prediction-log-db", type=Path, default=Path(".data/hightier_latency_probe/prediction_log.db"))
    parser.add_argument("--max-bets", type=int, default=None, help="Override hightier_scorer_max_bets_per_cycle.")
    parser.add_argument("--hot-lookback-hours", type=int, default=None, help="Override hot_feature_pool_lookback_hours.")
    parser.add_argument("--skip-feast", action="store_true", help="Stop after short-term feature stages.")
    parser.add_argument("--no-prediction-log", action="store_true", help="Skip prediction_log JSON/SQLite write.")
    parser.add_argument("--no-high-adt-only", action="store_true", help="Fetch all players instead of allowlist.")
    return parser.parse_args()


def _resolve_model_input(model_dir: Path, explicit: Path | None, filename: str) -> Path:
    """Resolve a model deploy input path."""
    if explicit is not None:
        return explicit.resolve()
    return (model_dir / "deploy_inputs" / filename).resolve()


def _configure_serving(args: argparse.Namespace) -> None:
    """Apply process-local serving config overrides for the probe."""
    cfg = default_hightier_serving_config()
    kwargs: dict[str, Any] = {
        "state_db_path": args.state_db.resolve(),
        "prediction_log_db_path": None if args.no_prediction_log else args.prediction_log_db.resolve(),
        "scorer_feast_repo_path": args.feast_repo.resolve(),
    }
    if args.max_bets is not None:
        kwargs["hightier_scorer_max_bets_per_cycle"] = int(args.max_bets)
    if args.hot_lookback_hours is not None:
        kwargs["hot_feature_pool_lookback_hours"] = int(args.hot_lookback_hours)
    set_hightier_serving_deploy_override(replace(cfg, **kwargs))


def _elapsed_since(t0: float) -> float:
    """Return rounded elapsed seconds."""
    return round(time.perf_counter() - t0, 4)


def _time_stage(timings: dict[str, float], name: str, fn: Any) -> Any:
    """Run a stage and record elapsed seconds."""
    t0 = time.perf_counter()
    out = fn()
    timings[name] = _elapsed_since(t0)
    return out


def _prepare_state(path: Path) -> sqlite3.Connection:
    """Initialize and open probe state DB."""
    init_state_db(path)
    return connect_state_db(path)


def _supplier_summary(plan: Any) -> dict[str, int]:
    """Summarize supplier route column counts."""
    return {
        "baseline": len(plan.baseline_cols),
        "feast_trial": len(plan.feast_trial_cols),
        "feast_mid": len(plan.feast_mid_cols),
        "mid_composite": len(plan.mid_composite_cols),
        "feast_slow": len(plan.feast_slow_cols),
        "short_term": len(plan.short_term_cols),
        "unknown": len(plan.unknown_cols),
    }


def _build_row_audits(staged: Any, bundle: Any, plan: Any, registry_snap: Any) -> list[Any]:
    """Build production-like row missing audits."""
    audits = compute_row_missing_audits(
        staged[list(bundle.feature_columns)],
        bundle.feature_columns,
        feast_mid_cols=plan.feast_mid_cols,
        feast_slow_cols=plan.feast_slow_cols,
        short_term_cols=plan.short_term_cols,
    )
    by_id = {row.feature_id: row for row in registry_snap.rows}
    runtime_inputs = {
        comp: runtime_inputs_from_registry(by_id.get(comp), comp)
        for comp in plan.mid_composite_cols
    }
    return enrich_row_audits_composite_upstream(
        staged,
        audits,
        composite_cols=plan.mid_composite_cols,
        runtime_inputs_by_feature=runtime_inputs,
    )


def _prediction_log_stage(args: argparse.Namespace, bundle: Any, staged: Any, prob: np.ndarray, audits: list[Any]) -> None:
    """Write production-like prediction log rows unless disabled."""
    if args.no_prediction_log:
        return
    init_prediction_log_db(args.prediction_log_db)
    append_hightier_prediction_log(
        args.prediction_log_db,
        scored_at=datetime.now(ZoneInfo(HK_TZ)).isoformat(),
        model_version=str(bundle.model_version),
        staged=staged,
        prob=prob,
        threshold=float(bundle.threshold),
        features=staged[list(bundle.feature_columns)],
        feature_columns=bundle.feature_columns,
        row_audits=audits,
        scoring_status="scored",
    )


def _load_probe_artifacts(
    timings: dict[str, float],
    model_dir: Path,
    allowlist_path: Path,
    *,
    no_high_adt_only: bool,
) -> tuple[Any, Any, Any, frozenset[Any]]:
    """Load model bundle, frozen registry, supplier plan, and optional ADT allowlist."""
    bundle = _time_stage(timings, "load_model", lambda: load_hightier_model_bundle(bundle_dir=model_dir))
    registry_snap = _time_stage(timings, "load_registry", lambda: load_frozen_registry_for_bundle(model_dir))
    plan = build_scorer_supplier_plan(registry_snap, bundle.feature_columns)
    assert_scorer_supplier_plan_or_raise(plan)
    if no_high_adt_only:
        allow_ids: frozenset[Any] = frozenset()
    else:
        allow_ids = _time_stage(timings, "load_allowlist", lambda: load_adt_allowlist_ids(allowlist_path))
    return bundle, registry_snap, plan, allow_ids


def _fetch_probe_batch(
    timings: dict[str, float],
    state_db: Path,
    *,
    high_adt_only: bool,
    allowlist_ids: frozenset[Any],
) -> Any | None:
    """Fetch one scoring batch from probe state DB; return None when empty."""
    conn = _prepare_state(state_db)
    try:
        return _time_stage(
            timings,
            "fetch_incremental_and_pool",
            lambda: _fetch_scoring_batch(
                conn,
                high_adt_only=high_adt_only,
                allowlist_ids=allowlist_ids,
            ),
        )
    finally:
        conn.close()


def _build_short_term_staged(timings: dict[str, float], batch: Any, mapping: Path, plan: Any) -> Any:
    """Build short-term staged features for the scoring batch."""
    return _time_stage(
        timings,
        "build_short_term_features",
        lambda: _build_staged_features(batch, mapping_parquet=mapping, supplier_plan=plan),
    )


def _run_feast_predict_pipeline(
    timings: dict[str, float],
    args: argparse.Namespace,
    staged: Any,
    bundle: Any,
    plan: Any,
    registry_snap: Any,
) -> Any:
    """Run Feast join, composites, predict, audits, and optional prediction log."""
    adapter = FeastSdkOnlineAdapter(feast_repo=args.feast_repo.resolve())
    staged, skipped, _ = _time_stage(
        timings,
        "feast_lookup_join",
        lambda: _attach_feast_mid_slow(
            staged,
            adapter,
            mid_columns=plan.feast_mid_cols,
            slow_columns=plan.feast_slow_cols,
            fail_fraction=default_hightier_serving_config().scorer_feast_entity_missing_fail_fraction,
        ),
    )
    timings["feast_skipped_rows"] = float(len(skipped))
    staged = _time_stage(
        timings,
        "mid_composites",
        lambda: attach_mid_term_composite_columns(staged, plan.mid_composite_cols),
    )
    assert_features_ready(staged, bundle.feature_columns)
    x_frame = coerce_categoricals(
        staged[list(bundle.feature_columns)].copy(),
        bundle.categorical_columns,
        dict(bundle.category_categories),
    )
    prob = _time_stage(timings, "predict", lambda: bundle.model.predict_proba(x_frame)[:, 1])
    audits = _time_stage(timings, "row_audits", lambda: _build_row_audits(staged, bundle, plan, registry_snap))
    _time_stage(timings, "prediction_log", lambda: _prediction_log_stage(args, bundle, staged, prob, audits))
    return staged


def _emit_no_batch_result(timings: dict[str, float]) -> int:
    """Print JSON for an empty scoring batch and return exit code 0."""
    print(json.dumps({"status": "no_batch", "timings": timings}, indent=2, sort_keys=True))
    return 0


def _emit_ok_result(
    timings: dict[str, float],
    total_t0: float,
    *,
    model_dir: Path,
    mapping: Path,
    allowlist: Path,
    batch: Any,
    staged: Any,
    plan: Any,
) -> int:
    """Print JSON for a successful probe cycle and return exit code 0."""
    timings["total"] = _elapsed_since(total_t0)
    result = {
        "status": "ok",
        "model_dir": str(model_dir),
        "mapping": str(mapping),
        "allowlist": str(allowlist),
        "rows": {"batch": len(batch.bets), "pool": len(batch.pool), "staged": len(staged)},
        "supplier_counts": _supplier_summary(plan),
        "timings": timings,
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def main() -> int:
    """Run one probe cycle and print JSON timings."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = _parse_args()
    model_dir = args.model_dir.resolve()
    mapping = _resolve_model_input(model_dir, args.mapping, "canonical_player_mapping.parquet")
    allowlist = _resolve_model_input(model_dir, args.allowlist, "adt_allowed_players_q0p99.parquet")
    _configure_serving(args)

    timings: dict[str, float] = {}
    total_t0 = time.perf_counter()
    bundle, registry_snap, plan, allow_ids = _load_probe_artifacts(
        timings, model_dir, allowlist, no_high_adt_only=args.no_high_adt_only
    )
    batch = _fetch_probe_batch(
        timings,
        args.state_db.resolve(),
        high_adt_only=not args.no_high_adt_only,
        allowlist_ids=allow_ids,
    )
    if batch is None:
        return _emit_no_batch_result(timings)

    staged = _build_short_term_staged(timings, batch, mapping, plan)
    if not args.skip_feast:
        staged = _run_feast_predict_pipeline(timings, args, staged, bundle, plan, registry_snap)
    return _emit_ok_result(
        timings,
        total_t0,
        model_dir=model_dir,
        mapping=mapping,
        allowlist=allowlist,
        batch=batch,
        staged=staged,
        plan=plan,
    )


if __name__ == "__main__":
    raise SystemExit(main())
