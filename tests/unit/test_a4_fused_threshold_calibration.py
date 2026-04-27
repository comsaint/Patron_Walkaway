"""Unit tests for A4 fused-score DEC-026 threshold calibration (trainer)."""

from __future__ import annotations

from pathlib import Path

import pytest
import joblib
import numpy as np
import pandas as pd

from trainer.serving import scorer
from trainer.training import trainer as trainer_mod


def test_pick_dec026_threshold_from_binary_scores_matches_dec026_contract() -> None:
    """High-recall feasible pick should not fallback."""
    y = np.array([0, 0, 0, 1, 1], dtype=float)
    s = np.array([0.1, 0.2, 0.3, 0.7, 0.9], dtype=float)
    out = trainer_mod.pick_dec026_threshold_from_binary_scores(
        y,
        s,
        recall_floor=0.01,
        min_alert_count=1,
        min_alerts_per_hour=None,
        window_hours=None,
        fbeta_beta=0.5,
    )
    assert not out.is_fallback
    assert 0.0 < out.threshold < 1.0


def test_pick_dec026_threshold_from_binary_scores_fallback_when_infeasible() -> None:
    y = np.array([0, 0, 1, 1], dtype=float)
    s = np.array([0.1, 0.2, 0.8, 0.9], dtype=float)
    out = trainer_mod.pick_dec026_threshold_from_binary_scores(
        y,
        s,
        recall_floor=0.01,
        min_alert_count=100,
        min_alerts_per_hour=None,
        window_hours=None,
        fbeta_beta=0.5,
    )
    assert out.is_fallback


class _ConstProba:
    def __init__(self, p: float) -> None:
        self._p = float(p)

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        n = len(X)
        p1 = np.full(n, self._p, dtype=float)
        return np.column_stack([1.0 - p1, p1])


def _write_a4_bundle(tmp_path: Path, *, threshold: float) -> None:
    joblib.dump(
        {
            "model": _ConstProba(0.6),
            "threshold": float(threshold),
            "features": ["f1"],
            "a4_enabled": True,
            "a4_fusion_mode": "product",
            "a4_candidate_cutoff": 0.3,
            "stage2_model": _ConstProba(1.0),
            "stage2_features": ["f1"],
        },
        tmp_path / "model.pkl",
    )
    (tmp_path / "model_version").write_text("test-a4-cal", encoding="utf-8")
    (tmp_path / "reason_code_map.json").write_text("{}", encoding="utf-8")
    (tmp_path / "feature_list.json").write_text('["f1"]', encoding="utf-8")


def test_scorer_a4_uses_bundle_threshold_on_fused_scores(tmp_path: Path) -> None:
    """Deployed threshold in bundle must apply to fused score (product), not stage-1 only."""
    _write_a4_bundle(tmp_path, threshold=0.5)
    art = scorer.load_dual_artifacts(tmp_path)
    old_flag = getattr(scorer.config, "A4_TWO_STAGE_ENABLE_INFERENCE", False)
    scorer.config.A4_TWO_STAGE_ENABLE_INFERENCE = True
    try:
        df = pd.DataFrame({"f1": [0.6, 0.6], "is_rated": [1, 1]})
        out = scorer._score_df(df, art, ["f1"], rated_threshold=0.5)
        # fused = 0.6 * 1.0 = 0.6; margin = 0.6 - 0.5 = 0.1
        assert float(out["margin"].iloc[0]) > 0.0
    finally:
        scorer.config.A4_TWO_STAGE_ENABLE_INFERENCE = old_flag


def test_scorer_a4_candidate_cutoff_uses_stage1_threshold_not_deployed(tmp_path: Path) -> None:
    """Stage-2 candidate mask must use cutoff from stage-1 threshold, not fused deployed threshold."""
    joblib.dump(
        {
            "model": _ConstProba(0.95),
            "threshold": 0.2,
            "features": ["f1"],
            "a4_enabled": True,
            "a4_fusion_mode": "product",
            "a4_candidate_cutoff": None,
            "a4_stage1_threshold_before_final_calibration": 0.8,
            "stage2_model": _ConstProba(0.5),
            "stage2_features": ["f1"],
        },
        tmp_path / "model.pkl",
    )
    (tmp_path / "model_version").write_text("test-cutoff", encoding="utf-8")
    (tmp_path / "reason_code_map.json").write_text("{}", encoding="utf-8")
    (tmp_path / "feature_list.json").write_text('["f1"]', encoding="utf-8")
    art = scorer.load_dual_artifacts(tmp_path)
    old_flag = getattr(scorer.config, "A4_TWO_STAGE_ENABLE_INFERENCE", False)
    scorer.config.A4_TWO_STAGE_ENABLE_INFERENCE = True
    try:
        df = pd.DataFrame({"f1": [0.95], "is_rated": [1]})
        out = scorer._score_df(df, art, ["f1"], rated_threshold=0.2)
        # cutoff = 0.8 * 0.9 = 0.72; stage1 0.95 >= 0.72 -> stage2 runs; fused = 0.95*0.5 = 0.475
        assert abs(float(out["score"].iloc[0]) - 0.475) < 1e-9
    finally:
        scorer.config.A4_TWO_STAGE_ENABLE_INFERENCE = old_flag


def test_backtester_score_df_uses_stage1_threshold_for_a4_candidate_cutoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trainer.training import backtester as bt_mod

    monkeypatch.setattr(bt_mod, "A4_TWO_STAGE_ENABLE_INFERENCE", True, raising=False)
    df = pd.DataFrame({"f1": [0.95], "is_rated": [True]})
    artifacts = {
        "rated": {
            "model": _ConstProba(0.95),
            "threshold": 0.2,
            "features": ["f1"],
            "a4_enabled": True,
            "a4_fusion_mode": "product",
            "a4_candidate_cutoff": None,
            "a4_stage1_threshold_before_final_calibration": 0.8,
            "stage2_model": _ConstProba(0.5),
            "stage2_features": ["f1"],
        }
    }
    out = bt_mod._score_df(df, artifacts)
    assert abs(float(out["score"].iloc[0]) - 0.475) < 1e-9


def test_snapshot_stage1_datasets_for_v2_shape() -> None:
    metrics = {
        "train_ap": 0.1,
        "val_ap": 0.2,
        "val_field_test_primary_score": 0.15,
        "test_ap": 0.3,
    }
    snap = trainer_mod._snapshot_stage1_datasets_for_v2(metrics)
    assert snap["train"]["ap"] == 0.1
    assert snap["val"]["ap"] == 0.2
    assert "field_test_primary_score" not in snap["val"]
    assert snap["test"]["ap"] == 0.3
