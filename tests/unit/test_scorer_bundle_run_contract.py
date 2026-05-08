"""W2: scorer load_dual_artifacts exposes bundle run contract (selection_mode, ...)."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from unittest import mock

import joblib
import pandas as pd
import pytest

import trainer.core.config as core_config
from trainer.serving import scorer as scorer_mod

_REPO = Path(__file__).resolve().parents[2]
_BUNDLE_SPEC_SRC = _REPO / "trainer" / "feature_spec" / "feature_candidates.yaml"


def _install_minimal_bundle_spec(root: Path) -> None:
    """Bundle-only scorer needs a loadable feature_spec.yaml next to model.pkl."""
    shutil.copy(_BUNDLE_SPEC_SRC, root / "feature_spec.yaml")


def test_load_dual_artifacts_run_contract_from_training_metrics() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        joblib.dump({"model": None, "threshold": 0.5, "features": []}, root / "model.pkl")
        _install_minimal_bundle_spec(root)
        (root / "training_metrics.json").write_text(
            json.dumps({"selection_mode": "field_test", "model_version": "t"}),
            encoding="utf-8",
        )
        art = scorer_mod.load_dual_artifacts(root)
    assert art["selection_mode"] == "field_test"
    assert art["selection_mode_source"] == "artifact_training_metrics.json"
    assert "production_neg_pos_ratio" in art


def test_load_dual_artifacts_run_contract_prefers_v2_when_both_exist() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        joblib.dump({"model": None, "threshold": 0.5, "features": []}, root / "model.pkl")
        _install_minimal_bundle_spec(root)
        (root / "training_metrics.json").write_text(
            json.dumps({"selection_mode": "legacy", "model_version": "v1"}),
            encoding="utf-8",
        )
        (root / "training_metrics.v2.json").write_text(
            json.dumps(
                {"schema_version": "training-metrics.v2", "selection_mode": "field_test"}
            ),
            encoding="utf-8",
        )
        art = scorer_mod.load_dual_artifacts(root)
    assert art["selection_mode"] == "field_test"
    assert art["selection_mode_source"] == "artifact_training_metrics.v2.json"


def test_load_dual_artifacts_run_contract_config_when_no_tm() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        joblib.dump({"model": None, "threshold": 0.5, "features": []}, root / "model.pkl")
        _install_minimal_bundle_spec(root)
        with mock.patch.object(core_config, "SELECTION_MODE", "legacy"):
            art = scorer_mod.load_dual_artifacts(root)
    assert art["selection_mode"] == "legacy"
    assert art["selection_mode_source"] == "config"


def test_build_features_pit_identity_strict_raises_without_cutoff_fallback() -> None:
    """Issue #19: SCORER_PIT_IDENTITY_STRICT must not silently fall back to cutoff_window."""
    bets = pd.DataFrame(
        {
            "bet_id": [1],
            "player_id": [10],
            "payout_complete_dtm": [pd.Timestamp("2025-06-01 12:00:00")],
        }
    )
    sessions = pd.DataFrame(
        {
            "session_id": [100],
            "session_start_dtm": [pd.Timestamp("2025-06-01 10:00:00")],
            "session_end_dtm": [pd.Timestamp("2025-06-01 11:00:00")],
        }
    )
    canonical_map = pd.DataFrame({"player_id": [10], "canonical_id": ["c10"]})
    cutoff = pd.Timestamp("2025-06-01 15:00:00")

    mock_cutoff = mock.MagicMock()
    mock_cutoff.exists.return_value = True
    prev = os.environ.get("SCORER_PIT_IDENTITY_STRICT")
    try:
        os.environ["SCORER_PIT_IDENTITY_STRICT"] = "1"
        with mock.patch.object(scorer_mod, "CANONICAL_MAPPING_CUTOFF_JSON", mock_cutoff):
            with mock.patch(
                "builtins.open",
                mock.mock_open(read_data='{"identity_mapping_mode": "pit_asof"}'),
            ):
                with mock.patch.object(
                    scorer_mod,
                    "build_pit_session_links_dataframe",
                    return_value=pd.DataFrame(),
                ):
                    with mock.patch.object(
                        scorer_mod,
                        "merge_pit_canonical_to_bets",
                        side_effect=RuntimeError("pit_merge_failed"),
                    ):
                        with pytest.raises(RuntimeError, match="SCORER_PIT_IDENTITY_STRICT"):
                            scorer_mod.build_features_for_scoring(
                                bets, sessions, canonical_map, cutoff
                            )
    finally:
        if prev is None:
            os.environ.pop("SCORER_PIT_IDENTITY_STRICT", None)
        else:
            os.environ["SCORER_PIT_IDENTITY_STRICT"] = prev
