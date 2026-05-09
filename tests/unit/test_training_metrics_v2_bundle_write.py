"""Unit tests for training_metrics v2 Phase A dual-write helpers."""

from __future__ import annotations

import json
from pathlib import Path

from trainer.core.training_metrics_v2_bundle_write import (
    SCHEMA_TRAINING_METRICS_V3,
    build_training_metrics_v2_payload,
    build_training_metrics_v3_payload,
    build_neg_pos_ratio_overview,
    write_training_metrics_v2_sidecars,
)


def test_build_v2_field_test_blocks() -> None:
    rated = {
        "val_precision": 0.8,
        "val_field_test_primary_score": 0.77,
        "val_field_test_primary_score_mode": "precision_prod_adjusted",
        "test_precision": 0.75,
        "test_precision_prod_adjusted": 0.46,
        "val_ap": 0.5,
    }
    root = {
        "selection_mode": "field_test",
        "production_neg_pos_ratio": 20.0,
        "rated": rated,
    }
    v2 = build_training_metrics_v2_payload(model_version="mv1", metrics_root=root)
    assert v2["datasets"]["val"]["field_test"]["precision_type"] == "prod_adjusted"
    assert abs(v2["datasets"]["val"]["field_test"]["precision"] - 0.77) < 1e-9
    assert v2["datasets"]["test"]["field_test"]["precision_type"] == "prod_adjusted"
    assert abs(v2["datasets"]["test"]["field_test"]["precision"] - 0.46) < 1e-9


def test_write_sidecars_writes_v3_v2_and_sidecars_and_metadata_paths(tmp_path: Path) -> None:
    rated = {
        "val_precision": 0.5,
        "val_field_test_primary_score": 0.5,
        "val_field_test_primary_score_mode": "precision_raw",
        "feature_importance": [{"name": "a", "importance_gain_pct": 1.0}],
        "gbm_bakeoff": {
            "schema_version": "a3_v2",
            "winner_backend": "xgboost",
            "selection_rule": "max_val_field_test_primary_score_then_val_ap_then_val_fbeta_05",
            "per_backend": {
                "xgboost": {
                    "val_ap": 0.1,
                    "val_precision": 0.5,
                    "val_field_test_primary_score": 0.5,
                    "val_field_test_primary_score_mode": "precision_raw",
                }
            },
        },
    }
    root = {
        "rated": rated,
        "selection_mode": "field_test",
        "production_neg_pos_ratio": None,
    }
    meta: dict = {"artifacts": {"training_metrics_path": str(tmp_path / "training_metrics.json")}}
    write_training_metrics_v2_sidecars(
        tmp_path,
        model_version="mv1",
        metrics_root=root,
        model_metadata=meta,
    )
    assert (tmp_path / "training_metrics.v3.json").is_file()
    assert (tmp_path / "training_metrics.v2.json").is_file()
    assert (tmp_path / "feature_importance.json").is_file()
    assert (tmp_path / "comparison_metrics.json").is_file()
    v3 = json.loads((tmp_path / "training_metrics.v3.json").read_text(encoding="utf-8"))
    assert v3.get("schema_version") == SCHEMA_TRAINING_METRICS_V3
    assert "objective_contract" in v3
    assert "segmentation" in v3
    v2 = json.loads((tmp_path / "training_metrics.v2.json").read_text(encoding="utf-8"))
    blob = json.dumps(v2)
    assert "feature_importance" not in blob
    assert "gbm_bakeoff" not in blob
    cm = json.loads((tmp_path / "comparison_metrics.json").read_text(encoding="utf-8"))
    assert cm["families"]["gbm_bakeoff"]["winner_id"] == "xgboost"
    assert "training_metrics_v3_path" in meta["artifacts"]
    assert "training_metrics_v2_path" in meta["artifacts"]
    assert "feature_importance_path" in meta["artifacts"]
    assert "comparison_metrics_path" in meta["artifacts"]


def test_comparison_metrics_preserves_backend_error_and_disposition(tmp_path: Path) -> None:
    rated = {
        "gbm_bakeoff": {
            "schema_version": "a3_v2",
            "winner_backend": "lightgbm",
            "selection_rule": "max_val_field_test_primary_score_then_val_ap_then_val_fbeta_05",
            "per_backend": {
                "lightgbm": {"val_precision": 0.7},
                "catboost": {
                    "error": "field-test constrained HPO allowed but val_window_hours missing/invalid",
                    "bakeoff_disposition": "reject",
                },
            },
        }
    }
    root = {"rated": rated, "selection_mode": "field_test"}
    write_training_metrics_v2_sidecars(
        tmp_path,
        model_version="mv1",
        metrics_root=root,
        model_metadata=None,
    )
    cm = json.loads((tmp_path / "comparison_metrics.json").read_text(encoding="utf-8"))
    cat = cm["families"]["gbm_bakeoff"]["candidates"]["catboost"]
    assert cat["candidate_id"] == "catboost"
    assert cat["bakeoff_disposition"] == "reject"
    assert "val_window_hours missing/invalid" in cat["error"]


def test_training_metrics_v2_map_json_is_valid() -> None:
    """Guardrail: map file stays parseable JSON (W1 deliverable)."""
    map_path = Path(__file__).resolve().parents[2] / "trainer" / "core" / "training_metrics_v2_map.json"
    data = json.loads(map_path.read_text(encoding="utf-8"))
    assert data.get("schema_version")
    assert isinstance(data.get("mappings"), list)


def test_v2_payload_includes_stage1_datasets_when_rated_carries_it() -> None:
    rated = {
        "train_ap": 0.11,
        "val_ap": 0.22,
        "test_ap": 0.33,
        "stage1_datasets": {
            "train": {"ap": 0.99},
            "val": {"ap": 0.88},
            "test": {"ap": 0.77},
        },
    }
    root = {"selection_mode": "field_test", "rated": rated}
    v2 = build_training_metrics_v2_payload(model_version="mv1", metrics_root=root)
    assert v2["datasets"]["train"]["ap"] == 0.11
    assert v2["stage1_datasets"]["train"]["ap"] == 0.99
    assert "stage1_datasets" not in v2["selection"]


def test_v3_neg_pos_ratio_overview_three_splits_and_segments() -> None:
    rated = {
        "train_samples": 100,
        "train_positives": 25,
        "val_neg_pos_ratio": 2.5,
        "val_samples": 40,
        "val_positives": 10,
        "test_neg_pos_ratio": 3.0,
        "test_samples": 20,
        "test_positives": 5,
    }
    seg_h = {
        "train_samples": 50,
        "train_positives": 10,
        "val_neg_pos_ratio": 4.0,
        "test_samples": 8,
        "test_positives": 2,
    }
    seg_l = {
        "train_neg_pos_ratio": 1.0,
        "val_samples": 30,
        "val_positives": 10,
    }
    root = {
        "selection_mode": "field_test",
        "rated": rated,
        "segment_high": seg_h,
        "segment_low": seg_l,
    }
    v3 = build_training_metrics_v3_payload(model_version="mv", metrics_root=root)
    ov = v3["neg_pos_ratio_overview"]
    assert ov["neg_pos_ratio_contract"] == "n_neg / n_pos"
    pm = ov["primary_model"]
    assert abs(pm["train"]["neg_pos_ratio"] - 3.0) < 1e-9
    assert pm["train"]["source"].startswith("derived_from")
    assert pm["val"]["neg_pos_ratio"] == 2.5
    assert pm["val"]["source"] == "rated.val_neg_pos_ratio"
    assert pm["test"]["neg_pos_ratio"] == 3.0
    assert len(ov["segments"]) == 2
    hi = next(s for s in ov["segments"] if s["segment"] == "high")
    assert hi["splits"]["train"]["neg_pos_ratio"] == 4.0
    assert hi["splits"]["val"]["neg_pos_ratio"] == 4.0
    assert abs(hi["splits"]["test"]["neg_pos_ratio"] - 3.0) < 1e-9
    lo = next(s for s in ov["segments"] if s["segment"] == "low")
    assert lo["splits"]["train"]["neg_pos_ratio"] == 1.0
    assert abs(lo["splits"]["val"]["neg_pos_ratio"] - 2.0) < 1e-9
    obs = v3["objective_contract"]["observed_split_ratios"]
    assert obs["train_neg_pos_ratio"] == pm["train"]["neg_pos_ratio"]
    assert obs["val_neg_pos_ratio"] == 2.5
    assert obs["test_neg_pos_ratio"] == 3.0
    assert build_neg_pos_ratio_overview({"rated": rated}, rated)["segments"] == []


def test_v3_field_test_optional_prod_adjusted_and_segmentation() -> None:
    rated = {
        "val_precision": 0.8,
        "val_field_test_primary_score": 0.77,
        "val_field_test_primary_score_mode": "precision_prod_adjusted",
        "test_precision": 0.75,
        "optuna_hpo_objective_mode": "field_test_dec026",
    }
    root = {
        "selection_mode": "field_test",
        "production_neg_pos_ratio": 15.0,
        "rated": rated,
        "high_roller_segmentation": {"primary_segment": "high"},
    }
    v3 = build_training_metrics_v3_payload(model_version="mv1", metrics_root=root)
    assert v3["schema_version"] == SCHEMA_TRAINING_METRICS_V3
    ft = v3["datasets"]["test"]["field_test"]
    assert ft["precision_prod_adjusted"] is None
    assert ft["precision_raw"] == 0.75
    assert "production_neg_pos_ratio" not in v3
    assert "ratio_assumption" not in v3["objective_contract"]
    assert v3["segmentation"]["enabled"] is True
    assert v3["segmentation"]["high_roller_segmentation"]["primary_segment"] == "high"


def test_v2_datasets_include_alert_density_columns_under_train_val_test() -> None:
    """train_/val_/test_ prefixed alert-density metrics map into datasets.* (no prefix)."""
    rated = {
        "train_ap": 0.1,
        "train_window_hours": 10.0,
        "train_alerts": 600,
        "train_alerts_per_hour": 60.0,
        "train_min_alerts_per_hour_objective": 50.0,
        "train_alerts_per_hour_meets_objective": True,
        "val_precision": 0.5,
        "val_window_hours": 2.0,
        "val_alerts": 80,
        "val_alerts_per_hour": 40.0,
        "val_min_alerts_per_hour_objective": 50.0,
        "val_alerts_per_hour_meets_objective": False,
        "test_ap": 0.2,
        "test_window_hours": 1.0,
        "test_alerts": 60,
        "test_alerts_per_hour": 60.0,
        "test_min_alerts_per_hour_objective": 50.0,
        "test_alerts_per_hour_meets_objective": True,
    }
    root = {"selection_mode": "field_test", "rated": rated}
    v2 = build_training_metrics_v2_payload(model_version="mv1", metrics_root=root)
    assert v2["datasets"]["train"]["window_hours"] == 10.0
    assert v2["datasets"]["train"]["alerts_per_hour_meets_objective"] is True
    assert v2["datasets"]["val"]["alerts_per_hour"] == 40.0
    assert v2["datasets"]["val"]["alerts_per_hour_meets_objective"] is False
    assert v2["datasets"]["test"]["min_alerts_per_hour_objective"] == 50.0
