"""Unit tests for training_metrics v2 Phase A dual-write helpers."""

from __future__ import annotations

import json
from pathlib import Path

from trainer.core.training_metrics_unified import SCHEMA_TRAINING_METRICS_UNIFIED
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
    assert v2["datasets"]["val"]["field_test"]["precision_type"] == "raw"
    assert abs(v2["datasets"]["val"]["field_test"]["precision"] - 0.8) < 1e-9
    assert v2["datasets"]["test"]["field_test"]["precision_type"] == "raw"
    assert abs(v2["datasets"]["test"]["field_test"]["precision"] - 0.75) < 1e-9


def test_write_sidecars_writes_single_unified_json_and_metadata_path(tmp_path: Path) -> None:
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
    assert (tmp_path / "training_metrics.json").is_file()
    assert not (tmp_path / "training_metrics.v3.json").is_file()
    assert not (tmp_path / "training_metrics.v2.json").is_file()
    assert not (tmp_path / "feature_importance.json").is_file()
    assert not (tmp_path / "comparison_metrics.json").is_file()
    unified = json.loads((tmp_path / "training_metrics.json").read_text(encoding="utf-8"))
    assert unified.get("schema_version") == SCHEMA_TRAINING_METRICS_UNIFIED
    v3 = unified["contract_v3"]
    assert v3.get("schema_version") == SCHEMA_TRAINING_METRICS_V3
    assert "objective_contract" in v3
    assert "segmentation" in v3
    v2 = unified["contract_v2"]
    blob = json.dumps(v2)
    assert "feature_importance" not in blob
    assert "gbm_bakeoff" not in blob
    cm = unified["comparison_metrics"]
    assert cm["families"]["gbm_bakeoff"]["winner_id"] == "xgboost"
    assert "training_metrics_path" in meta["artifacts"]
    assert "training_metrics_v3_path" not in meta["artifacts"]
    assert "training_metrics_v2_path" not in meta["artifacts"]
    assert "feature_importance_path" not in meta["artifacts"]
    assert "comparison_metrics_path" not in meta["artifacts"]


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
    unified = json.loads((tmp_path / "training_metrics.json").read_text(encoding="utf-8"))
    cm = unified["comparison_metrics"]
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
        "p10_model": seg_h,
        "low_value_model": seg_l,
        "high_roller_segmentation": {
            "high_roller_quantile": 0.90,
            "tail_model_key": "p10_model",
            "high_roller_segment_train_rated_unique_canonical_high": 9,
            "high_roller_segment_valid_rated_unique_canonical_high": 4,
            "high_roller_segment_test_rated_unique_canonical_high": 2,
            "high_roller_segment_train_rated_unique_canonical_low": 40,
            "high_roller_segment_valid_rated_unique_canonical_low": 12,
            "high_roller_segment_test_rated_unique_canonical_low": 3,
        },
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
    hi = next(s for s in ov["segments"] if s["segment"] == "p10_model")
    assert hi["splits"]["train"]["neg_pos_ratio"] == 4.0
    assert hi["splits"]["val"]["neg_pos_ratio"] == 4.0
    assert abs(hi["splits"]["test"]["neg_pos_ratio"] - 3.0) < 1e-9
    assert hi["unique_canonical_rated"]["train"] == 9
    assert hi["unique_canonical_rated"]["val"] == 4
    assert hi["unique_canonical_rated"]["test"] == 2
    lo = next(s for s in ov["segments"] if s["segment"] == "low_value_model")
    assert lo["splits"]["train"]["neg_pos_ratio"] == 1.0
    assert abs(lo["splits"]["val"]["neg_pos_ratio"] - 2.0) < 1e-9
    assert lo["unique_canonical_rated"]["train"] == 40
    assert lo["unique_canonical_rated"]["val"] == 12
    assert lo["unique_canonical_rated"]["test"] == 3
    obs = v3["objective_contract"]["observed_split_ratios"]
    assert obs["train_neg_pos_ratio"] == pm["train"]["neg_pos_ratio"]
    assert obs["val_neg_pos_ratio"] == 2.5
    assert obs["test_neg_pos_ratio"] == 3.0
    assert build_neg_pos_ratio_overview({"rated": rated}, rated)["segments"] == []


def test_v3_field_test_raw_only_and_segmentation() -> None:
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
        "high_roller_segmentation": {
            "serving_root_segment": "p10_model",
            "tail_model_key": "p10_model",
        },
    }
    v3 = build_training_metrics_v3_payload(model_version="mv1", metrics_root=root)
    assert v3["schema_version"] == SCHEMA_TRAINING_METRICS_V3
    ft_val = v3["datasets"]["val"]["field_test"]
    ft_test = v3["datasets"]["test"]["field_test"]
    assert ft_val["precision_raw"] == 0.8
    assert ft_val["precision_type"] == "raw"
    assert "precision_prod_adjusted" not in ft_val
    assert ft_test["precision_raw"] == 0.75
    assert ft_test["precision_type"] == "raw"
    assert "precision_prod_adjusted" not in ft_test
    assert "production_neg_pos_ratio" not in v3
    assert "ratio_assumption" not in v3["objective_contract"]
    assert v3["segmentation"]["enabled"] is True
    assert v3["segmentation"]["high_roller_segmentation"]["serving_root_segment"] == "p10_model"


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
