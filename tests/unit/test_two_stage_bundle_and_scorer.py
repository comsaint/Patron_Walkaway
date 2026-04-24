from __future__ import annotations

import inspect
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

from trainer.serving import scorer
from trainer.training import trainer as trainer_mod


class _LinearModel:
    def __init__(self, feature: str = "f1", slope: float = 1.0) -> None:
        self.feature = feature
        self.slope = slope

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        v = np.asarray(X[self.feature], dtype=np.float64).reshape(-1)
        p1 = np.clip(v * self.slope, 0.0, 1.0)
        return np.column_stack([1.0 - p1, p1])


class _BrokenModel:
    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        raise RuntimeError("boom")


def _write_bundle(tmp_path: Path, payload: dict) -> None:
    joblib.dump(payload, tmp_path / "model.pkl")
    (tmp_path / "model_version").write_text("test-version", encoding="utf-8")
    (tmp_path / "reason_code_map.json").write_text("{}", encoding="utf-8")
    (tmp_path / "feature_list.json").write_text('["f1"]', encoding="utf-8")


def test_load_dual_artifacts_backward_compatible_defaults(tmp_path: Path) -> None:
    _write_bundle(
        tmp_path,
        {
            "model": _LinearModel(),
            "threshold": 0.5,
            "features": ["f1"],
        },
    )
    art = scorer.load_dual_artifacts(tmp_path)
    rated = art["rated"]
    assert rated is not None
    assert rated["a4_enabled"] is False
    assert rated["stage2_model"] is None


def test_scorer_score_df_two_stage_fallback_to_stage1_on_stage2_error(tmp_path: Path) -> None:
    _write_bundle(
        tmp_path,
        {
            "model": _LinearModel(),
            "threshold": 0.5,
            "features": ["f1"],
            "a4_enabled": True,
            "a4_fusion_mode": "product",
            "a4_candidate_cutoff": 0.2,
            "stage2_model": _BrokenModel(),
            "stage2_features": ["f1"],
        },
    )
    art = scorer.load_dual_artifacts(tmp_path)
    old_flag = getattr(scorer.config, "A4_TWO_STAGE_ENABLE_INFERENCE", False)
    scorer.config.A4_TWO_STAGE_ENABLE_INFERENCE = True
    try:
        df = pd.DataFrame(
            {
                "f1": [0.1, 0.7],
                "is_rated": [1, 1],
            }
        )
        out = scorer._score_df(df, art, ["f1"], rated_threshold=0.5)
        assert np.allclose(out["score"].to_numpy(dtype=float), np.asarray([0.1, 0.7], dtype=float))
    finally:
        scorer.config.A4_TWO_STAGE_ENABLE_INFERENCE = old_flag


def test_train_single_rated_model_releases_a4_temp_matrices_after_metrics() -> None:
    src = inspect.getsource(trainer_mod.train_single_rated_model)
    anchor = "Peak-RAM cleanup: A4 builds several large stage-1 / stage-2 matrices"
    i_anchor = src.find(anchor)
    assert i_anchor > 0, "A4 cleanup anchor must exist in train_single_rated_model"
    window = src[i_anchor : i_anchor + 900]
    assert "_x_tr_s1 = None" in window
    assert "_x2_tr = None" in window
    assert "_x_vl_s1 = None" in window
    assert "_x_te_s1 = None" in window
    assert "gc.collect()" in window

