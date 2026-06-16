"""Tests for player-game grain offline experiment decision gates."""

from __future__ import annotations

import json
import pickle
from pathlib import Path

import pandas as pd
import pytest
from sklearn.linear_model import LogisticRegression

from trainer_hightier.feature_experiment import player_game_grain_experiment as pge


def _write_pg_arm(
    arm_dir: Path,
    val_df: pd.DataFrame,
    *,
    feature_columns: tuple[str, ...],
) -> None:
    """Write minimal player-game model artifacts for decision-gate tests."""

    arm_dir.mkdir(parents=True, exist_ok=True)
    model = LogisticRegression()
    x = val_df.loc[:, list(feature_columns)]
    y = val_df["player_game_label"].astype(int)
    model.fit(x, y)
    pkt = {
        "model": model,
        "feature_columns": list(feature_columns),
        "threshold": 0.5,
        "category_categories": {},
    }
    (arm_dir / "model.pkl").write_bytes(pickle.dumps(pkt))
    (arm_dir / "training_metrics.json").write_text(
        json.dumps({"threshold": 0.5}),
        encoding="utf-8",
    )


def test_decision_gate_passes_when_pg_beats_baseline_at_k(tmp_path: Path) -> None:
    """Serving migration gate passes when PG recall/precision at baseline K meet baseline."""

    splits_dir = tmp_path / "player_game_splits"
    splits_dir.mkdir()
    val_df = pd.DataFrame(
        {
            "player_game_label": [1, 1, 0, 0],
            "pg__wager_sum": [10.0, 8.0, 1.0, 2.0],
        },
    )
    val_df.to_parquet(splits_dir / "val.parquet", index=False)
    arm_dir = tmp_path / "player_game_composition"
    _write_pg_arm(arm_dir, val_df, feature_columns=("pg__wager_sum",))

    baseline = {"val_precision": 0.5, "val_recall": 0.5, "val_alerts": 2}
    decision = pge._decision_gate(
        baseline,
        arm_dir,
        splits_dir / "val.parquet",
        baseline_k=2,
    )
    assert decision["proceed_to_serving_migration"] is True
    assert decision["player_game_recall_at_k"] >= baseline["val_recall"]
    assert decision["player_game_precision_at_k"] >= baseline["val_precision"]


def test_decision_gate_fails_when_pg_recall_below_baseline(tmp_path: Path) -> None:
    """Gate blocks migration when top-K recall falls short of baseline."""

    splits_dir = tmp_path / "player_game_splits"
    splits_dir.mkdir()
    val_df = pd.DataFrame(
        {
            "player_game_label": [1, 1, 0, 0],
            "pg__wager_sum": [10.0, 8.0, 1.0, 2.0],
        },
    )
    val_df.to_parquet(splits_dir / "val.parquet", index=False)
    arm_dir = tmp_path / "player_game_composition"
    _write_pg_arm(arm_dir, val_df, feature_columns=("pg__wager_sum",))

    baseline = {"val_precision": 0.0, "val_recall": 1.0, "val_alerts": 1}
    decision = pge._decision_gate(
        baseline,
        arm_dir,
        splits_dir / "val.parquet",
        baseline_k=1,
    )
    assert decision["proceed_to_serving_migration"] is False
    assert decision["player_game_recall_at_k"] < baseline["val_recall"]


def test_refresh_player_game_decision_report_skips_missing_arms(tmp_path: Path) -> None:
    """Refresh-only mode marks arms skipped when model or val split is absent."""

    out_dir = tmp_path / "w2_out"
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(
        json.dumps({"val_precision": 0.5, "val_recall": 0.5, "val_alerts": 1}),
        encoding="utf-8",
    )
    report = pge.refresh_player_game_decision_report(
        out_dir=out_dir,
        baseline_report_json=baseline_path,
    )
    assert report["experiment_kind"] == "player_game_grain_w2_v1_b1_refresh"
    assert report["decision"] is None
    assert report["player_game_arms"]["player_game_composition"]["skipped"] is True
    assert report["player_game_arms"]["player_game_baseline_parity"]["skipped"] is True


def test_refresh_player_game_decision_report_evaluates_present_arm(tmp_path: Path) -> None:
    """Refresh recomputes decision for an arm with model.pkl and val split on disk."""

    out_dir = tmp_path / "w2_out"
    splits_dir = out_dir / "player_game_splits"
    splits_dir.mkdir(parents=True)
    val_df = pd.DataFrame(
        {
            "player_game_label": [1, 1, 0, 0],
            "pg__wager_sum": [10.0, 8.0, 1.0, 2.0],
        },
    )
    val_df.to_parquet(splits_dir / "val.parquet", index=False)
    arm_dir = out_dir / "player_game_composition"
    _write_pg_arm(arm_dir, val_df, feature_columns=("pg__wager_sum",))

    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(
        json.dumps({"val_precision": 0.5, "val_recall": 0.5, "val_alerts": 2}),
        encoding="utf-8",
    )
    report = pge.refresh_player_game_decision_report(
        out_dir=out_dir,
        baseline_report_json=baseline_path,
    )
    arm = report["player_game_arms"]["player_game_composition"]
    assert arm["skipped"] is False
    assert report["decision"] is not None
    assert report["decision"]["proceed_to_serving_migration"] is True
    assert "Serving migration gate = decision_baseline_parity" in report["method_note"]


def test_load_baseline_report_rejects_non_object(tmp_path: Path) -> None:
    """Baseline JSON must deserialize to a dict."""

    bad = tmp_path / "bad.json"
    bad.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ValueError, match="JSON object"):
        pge._load_baseline_report(bad)
