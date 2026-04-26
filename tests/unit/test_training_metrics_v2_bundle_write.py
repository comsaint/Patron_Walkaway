"""Unit tests for training_metrics v2 Phase A dual-write helpers."""

from __future__ import annotations

import json
from pathlib import Path

from trainer.core.training_metrics_v2_bundle_write import (
    build_training_metrics_v2_payload,
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


def test_write_sidecars_writes_three_files_and_metadata_paths(tmp_path: Path) -> None:
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
    assert (tmp_path / "training_metrics.v2.json").is_file()
    assert (tmp_path / "feature_importance.json").is_file()
    assert (tmp_path / "comparison_metrics.json").is_file()
    v2 = json.loads((tmp_path / "training_metrics.v2.json").read_text(encoding="utf-8"))
    blob = json.dumps(v2)
    assert "feature_importance" not in blob
    assert "gbm_bakeoff" not in blob
    cm = json.loads((tmp_path / "comparison_metrics.json").read_text(encoding="utf-8"))
    assert cm["families"]["gbm_bakeoff"]["winner_id"] == "xgboost"
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
