"""baseline_models reference metrics: v2-first bundle merge."""

from __future__ import annotations

import json
from pathlib import Path

from baseline_models.src.eval.reference_model import load_training_metrics_reference


def test_reference_model_loads_pat_from_v2_merged_flat(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "training_metrics.json").write_text(
        json.dumps({"rated": {}}),
        encoding="utf-8",
    )
    (bundle / "training_metrics.v2.json").write_text(
        json.dumps(
            {
                "schema_version": "training-metrics.v2",
                "datasets": {
                    "test": {
                        "ap": 0.55,
                        "precision_at_recall_0.01": 0.42,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    ref = load_training_metrics_reference(bundle / "training_metrics.json", "rated")
    assert ref.get("status") == "loaded"
    assert ref.get("test_ap") == 0.55
    assert ref.get("test_precision_at_recall_0.01") == 0.42
