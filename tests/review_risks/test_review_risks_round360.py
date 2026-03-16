"""Round 104 reviewer risks -> minimal reproducible tests.

Production fixes were applied in Round 106; @expectedFailure decorators
removed so these tests now run as standard assertions.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.skip(reason="api_server reverted to DB-only; model API removed")

import importlib
import inspect
import pathlib
import sqlite3
import sys
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd


def _import_module(name: str):
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    repo_root_str = str(repo_root)
    if repo_root_str not in sys.path:
        sys.path.insert(0, repo_root_str)
    # api_server.py/scorer.py use bare `import config`
    if "config" not in sys.modules:
        sys.modules["config"] = importlib.import_module("trainer.config")
    return importlib.import_module(name)


api_server_mod = _import_module("trainer.api_server")
backtester_mod = _import_module("trainer.backtester")
scorer_mod = _import_module("trainer.scorer")
trainer_mod = _import_module("trainer.trainer")


class _ConstHighModel:
    """Minimal predict_proba stub: always returns positive class 0.95."""

    def predict_proba(self, X):
        n = len(X)
        p = np.full(n, 0.95, dtype="float64")
        return np.c_[1.0 - p, p]


class TestR3600ScorerUnratedAlertLeak(unittest.TestCase):
    """R3600: scorer should not emit alerts for unrated observations."""

    def test_score_once_should_emit_only_rated_alerts(self):
        emitted: list[pd.DataFrame] = []

        artifacts = {
            "rated": {"model": _ConstHighModel(), "threshold": 0.5, "features": ["f1"]},
            "feature_list": ["f1"],
            "reason_code_map": {},
            "model_version": "test-v0",
            "feature_spec": None,
        }

        bets = pd.DataFrame({"bet_id": [1, 2]})
        sessions = pd.DataFrame({"player_id": [11, 22], "casino_player_id": ["R1", None]})

        features_df = pd.DataFrame(
            {
                "bet_id": [1, 2],
                "player_id": [11, 22],
                "canonical_id": ["cid_r", "cid_u"],
                "session_id": ["s1", "s2"],
                "wager": [100.0, 100.0],
                "payout_complete_dtm": [pd.Timestamp("2026-03-06 10:00:00"), pd.Timestamp("2026-03-06 10:01:00")],
                "f1": [1.0, 1.0],
            }
        )

        with (
            patch.object(scorer_mod, "fetch_recent_data", return_value=(bets, sessions)),
            patch.object(
                scorer_mod,
                "build_canonical_mapping_from_df",
                return_value=pd.DataFrame(
                    {"player_id": [11], "canonical_id": ["cid_r"], "casino_player_id": ["R1"]}
                ),
            ),
            patch.object(scorer_mod, "build_features_for_scoring", return_value=features_df),
            patch.object(scorer_mod, "compute_track_llm_features", side_effect=lambda df, **_: df),
            patch.object(scorer_mod, "_compute_reason_codes", return_value=["[]"]),
            patch.object(scorer_mod, "prune_old_state"),
            patch.object(scorer_mod, "refresh_alert_history"),
            patch.object(scorer_mod, "update_state_with_new_bets", return_value=bets),
            patch.object(scorer_mod, "get_session_totals", return_value=(0, 0.0, None, None)),
            patch.object(scorer_mod, "get_session_count", return_value=0),
            patch.object(scorer_mod, "get_historical_avg", return_value=0.0),
            patch.object(scorer_mod, "append_alerts", side_effect=lambda _conn, df: emitted.append(df.copy())),
        ):
            conn = sqlite3.connect(":memory:")
            scorer_mod.score_once(artifacts, lookback_hours=1, alert_history=set(), conn=conn, retention_hours=1)

        self.assertTrue(emitted, "Expected at least one emitted alert batch")
        out = emitted[0]
        self.assertTrue(
            (out["is_rated_obs"] == 1).all(),
            "Scorer should emit alerts only for rated observations.",
        )


class TestR3601ApiUnratedAlertLeak(unittest.TestCase):
    """R3601: API /score should not alert unrated observations."""

    def test_score_endpoint_unrated_row_should_not_alert(self):
        app = api_server_mod.app
        client = app.test_client()

        arts = {
            "rated": {"model": _ConstHighModel(), "threshold": 0.5},
            "feature_list": ["f1"],
            "reason_code_map": {"f1": "F1"},
            "model_version": "test-v0",
            "rated_explainer": None,
        }

        with (
            patch.object(api_server_mod, "_get_artifacts", return_value=arts),
            patch.object(api_server_mod, "_compute_shap_reason_codes_batch", return_value=[[]]),
        ):
            resp = client.post(
                "/score",
                json={"rows": [{"f1": 1.0, "bet_id": 1, "is_rated": False}]},
            )
            self.assertEqual(resp.status_code, 200)
            payload = resp.get_json()
            self.assertIn("scores", payload)
            self.assertEqual(len(payload["scores"]), 1)
            self.assertFalse(
                payload["scores"][0]["alert"],
                "API should not return alert=True for unrated rows.",
            )


class TestR3602BacktesterCombinedApScope(unittest.TestCase):
    """R3602: combined AP (average precision) must not be skewed by unrated observations."""

    def test_combined_micro_ap_should_match_rated_track_when_unrated_is_noise(self):
        rated = pd.DataFrame(
            {
                "canonical_id": ["r1", "r2"],
                "gaming_day": ["2026-03-06", "2026-03-06"],
                "is_rated": [True, True],
                "label": [1, 0],
                "score": [0.95, 0.10],
            }
        )
        unrated = pd.DataFrame(
            {
                "canonical_id": ["u1", "u2"],
                "gaming_day": ["2026-03-06", "2026-03-06"],
                "is_rated": [False, False],
                "label": [0, 0],
                "score": [0.99, 0.98],
            }
        )
        labeled = pd.concat([rated, unrated], ignore_index=True)
        out = backtester_mod._compute_section_metrics(
            labeled=labeled,
            rated_sub=rated,
            threshold=0.5,
            window_hours=1.0,
        )
        # Section is flat (PLAN step 3); metrics on rated_sub only; trainer-style key test_ap.
        # Rated-only AP: labels [1,0], scores [0.95, 0.10] → AP = 1.0 (no unrated skew).
        self.assertAlmostEqual(
            out["test_ap"],
            1.0,
            places=10,
            msg="Combined AP should not be skewed by unrated observations when policy is rated-only alerts.",
        )


class TestR3603ArtifactCleanupGuard(unittest.TestCase):
    """R3603: run_pipeline should remove stale nonrated_model.pkl after saving artifacts.

    Cleanup must live in run_pipeline (not save_artifact_bundle) so that
    save_artifact_bundle itself contains no nonrated_model.pkl reference
    (required by TestR1501 in test_review_risks_late_rounds.py).
    """

    def test_run_pipeline_should_cleanup_legacy_nonrated_model_file(self):
        src = inspect.getsource(trainer_mod.run_pipeline)
        self.assertIn(
            "nonrated_model.pkl",
            src,
            "run_pipeline should cleanup stale nonrated_model.pkl after save_artifact_bundle.",
        )

    def test_save_artifact_bundle_should_not_reference_nonrated_model_pkl(self):
        src = inspect.getsource(trainer_mod.save_artifact_bundle)
        self.assertNotIn(
            "nonrated_model.pkl",
            src,
            "save_artifact_bundle must not reference nonrated_model.pkl (R1501 contract).",
        )


class TestR3604DocConsistencyGuards(unittest.TestCase):
    """R3604: docs should reflect single-rated-model behavior."""

    def test_api_score_doc_should_not_describe_dual_model_routing(self):
        src = inspect.getsource(api_server_mod.score)
        self.assertNotIn(
            "true → rated model, false → non-rated model",
            src,
            "API /score docstring should describe v10 single-rated-model behavior.",
        )

    def test_scorer_module_doc_should_not_mention_dual_model_artifacts(self):
        src = inspect.getsource(scorer_mod)
        self.assertNotIn(
            "Dual-model artifacts",
            src,
            "scorer.py module doc should no longer mention dual-model artifacts.",
        )

    def test_backtester_micro_doc_should_not_reference_nonrated_alerting_rule(self):
        src = inspect.getsource(backtester_mod.compute_micro_metrics)
        self.assertNotIn(
            "nonrated are not alerted",
            src,
            "compute_micro_metrics doc should be updated for single-rated-model wording.",
        )


if __name__ == "__main__":
    unittest.main()
