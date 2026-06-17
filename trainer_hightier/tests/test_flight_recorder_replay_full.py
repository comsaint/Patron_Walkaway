"""Integration tests for flight recorder offline replay."""

from __future__ import annotations

import pickle
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sklearn.dummy import DummyClassifier

from trainer_hightier.config import FEATURE_CANDIDATE_REGISTRY_SNAPSHOT_FILENAME
from trainer_hightier.serving.flight_recorder.replay_score import run_score_replay
from trainer_hightier.serving.flight_recorder.replay_validator import run_validator_replay
from trainer_hightier.serving.replay_recording_bundle import run_full_analysis


def _write_model_bundle(tmp_path: Path, *, feature_cols: tuple[str, ...]) -> Path:
    """Minimal model bundle for replay tests."""
    model = DummyClassifier(strategy="constant", constant=1)
    model.fit([[0.0], [1.0]], [0, 1])
    bundle_dir = tmp_path / "models"
    bundle_dir.mkdir()
    payload = {
        "model": model,
        "feature_columns": list(feature_cols),
        "threshold": 0.5,
        "categorical_columns": [],
        "category_categories": {},
    }
    (bundle_dir / "model.pkl").write_bytes(pickle.dumps(payload))
    (bundle_dir / "model_version").write_text("replay-test\n", encoding="utf-8")
    (bundle_dir / FEATURE_CANDIDATE_REGISTRY_SNAPSHOT_FILENAME).write_text(
        "registry_version: test\nfeatures: []\n",
        encoding="utf-8",
    )
    return bundle_dir


def test_score_replay_matches_stage_09(tmp_path: Path) -> None:
    """Score replay achieves 100% match when stage_09 matches model output."""
    features = ("wager", "player_id")
    bundle_dir = _write_model_bundle(tmp_path, feature_cols=features)
    model = pickle.loads((bundle_dir / "model.pkl").read_bytes())["model"]
    X = pd.DataFrame({"wager": [100.0, 200.0], "player_id": [1, 2]})
    prob = model.predict_proba(X)[:, 1]
    root = tmp_path / "recording"
    cycle = root / "cycles" / "scorer" / "cycle_000001" / "stages"
    cycle.mkdir(parents=True)
    X.to_parquet(cycle / "stage_08_model_feature_matrix.parquet", index=False)
    scores = X.assign(score=prob)
    scores.to_parquet(cycle / "stage_09_scores.parquet", index=False)
    report = run_score_replay(root, bundle_dir)
    assert report["overall_match_rate"] == 1.0
    assert report["total_rows"] == 2


def test_validator_replay_skips_without_trace(tmp_path: Path) -> None:
    """Validator replay returns empty comparison without cycles."""
    root = tmp_path / "recording"
    report = run_validator_replay(root)
    assert report["total_compared"] == 0


def test_full_analysis_writes_reports(tmp_path: Path) -> None:
    """Full analysis writes score and validator replay JSON under output_dir."""
    features = ("wager",)
    bundle_dir = _write_model_bundle(tmp_path, feature_cols=features)
    root = tmp_path / "recording"
    stage = root / "cycles" / "scorer" / "cycle_000001" / "stages"
    stage.mkdir(parents=True)
    pd.DataFrame({"wager": [1.0]}).to_parquet(
        stage / "stage_08_model_feature_matrix.parquet",
        index=False,
    )
    pd.DataFrame({"wager": [1.0], "score": [1.0]}).to_parquet(
        stage / "stage_09_scores.parquet",
        index=False,
    )
    (root / "MANIFEST.json").write_text(
        '{"schema_version":"flight_recorder_v1","files":[]}',
        encoding="utf-8",
    )
    out = tmp_path / "analysis"
    reports = run_full_analysis(root, out, model_bundle_dir=bundle_dir)
    assert (out / "score_replay_diff_report.json").is_file()
    assert (out / "validator_replay_diff_report.json").is_file()
    assert reports.get("score_replay") is not None
