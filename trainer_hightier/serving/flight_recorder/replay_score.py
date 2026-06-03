"""Offline score replay from captured scorer stage artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from trainer_hightier.serving.feature_builder import prepare_lgbm_feature_matrix
from trainer_hightier.serving.model_bundle import HightierModelBundle, load_hightier_model_bundle


def _load_production_scores(cycle_dir: Path) -> pd.Series | None:
    """Load production scores from ``stage_09_scores.parquet`` when present."""
    path = cycle_dir / "stages" / "stage_09_scores.parquet"
    if not path.is_file():
        return None
    frame = pd.read_parquet(path)
    if "score" not in frame.columns:
        return None
    return pd.to_numeric(frame["score"], errors="coerce")


def replay_one_scorer_cycle(
    cycle_dir: Path,
    bundle: HightierModelBundle,
) -> dict[str, Any]:
    """Replay scores for one cycle; return per-row comparison stats."""
    stage08 = cycle_dir / "stages" / "stage_08_model_feature_matrix.parquet"
    if not stage08.is_file():
        return {"cycle": cycle_dir.name, "status": "skipped", "detail": "no stage_08"}
    features = pd.read_parquet(stage08)
    if features.empty:
        return {"cycle": cycle_dir.name, "status": "skipped", "detail": "empty stage_08"}
    matrix = prepare_lgbm_feature_matrix(
        features,
        feature_columns=bundle.feature_columns,
        categorical_columns=bundle.categorical_columns,
        category_categories=dict(bundle.category_categories),
    )
    replay_prob = bundle.model.predict_proba(matrix)[:, 1]
    prod = _load_production_scores(cycle_dir)
    n = len(matrix)
    if prod is None or len(prod) != n:
        return {
            "cycle": cycle_dir.name,
            "status": "replay_only",
            "n_rows": n,
            "replay_mean_score": float(np.mean(replay_prob)),
        }
    prod_arr = prod.to_numpy(dtype=np.float64)
    diff = np.abs(replay_prob - prod_arr)
    tol = 1e-6
    n_match = int((diff <= tol).sum())
    return {
        "cycle": cycle_dir.name,
        "status": "ok",
        "n_rows": n,
        "n_match": n_match,
        "match_rate": float(n_match / n) if n else 1.0,
        "max_abs_diff": float(diff.max()) if n else 0.0,
        "mean_abs_diff": float(diff.mean()) if n else 0.0,
    }


def run_score_replay(
    recording_root: Path,
    model_bundle_dir: Path,
) -> dict[str, Any]:
    """Replay all scorer cycles and aggregate match statistics."""
    bundle = load_hightier_model_bundle(bundle_dir=model_bundle_dir)
    scorer_dir = recording_root / "cycles" / "scorer"
    per_cycle: list[dict[str, Any]] = []
    if scorer_dir.is_dir():
        for cycle_dir in sorted(scorer_dir.glob("cycle_*")):
            per_cycle.append(replay_one_scorer_cycle(cycle_dir, bundle))
    ok_cycles = [c for c in per_cycle if c.get("status") == "ok"]
    total_rows = sum(int(c.get("n_rows", 0)) for c in ok_cycles)
    total_match = sum(int(c.get("n_match", 0)) for c in ok_cycles)
    return {
        "model_version": bundle.model_version,
        "threshold": bundle.threshold,
        "n_cycles": len(per_cycle),
        "n_cycles_compared": len(ok_cycles),
        "total_rows": total_rows,
        "total_match": total_match,
        "overall_match_rate": float(total_match / total_rows) if total_rows else None,
        "per_cycle": per_cycle,
    }


def write_score_replay_report(output_dir: Path, report: dict[str, Any]) -> Path:
    """Write ``score_replay_diff_report.json`` under *output_dir*."""
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "score_replay_diff_report.json"
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return path
