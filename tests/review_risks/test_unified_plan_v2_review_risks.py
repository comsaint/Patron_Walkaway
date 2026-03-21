"""Unified Plan v2 (2026-03-21) — Code Review risks as MRE tests / contracts.

Maps STATUS.md §「Code Review：統一計劃 v2 — T1 + T2」items 1–8.
Tests-only: no production changes. Some tests document current buggy behaviour;
others assert static contracts (ordering, optuna not logged, etc.).
"""

from __future__ import annotations

import logging
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import trainer.scorer as scorer_mod
from trainer.training import backtester as backtester_mod

REPO_ROOT = Path(__file__).resolve().parents[2]
SCORER_SRC = REPO_ROOT / "trainer" / "serving" / "scorer.py"
BACKTESTER_SRC = REPO_ROOT / "trainer" / "training" / "backtester.py"
# Windows: Path.exists is not patchable; force scorer to skip persisted canonical artifact load.
_FAKE_CANONICAL_PARQUET = REPO_ROOT / "__unified_v2_test__no_canonical_mapping.parquet"
_FAKE_CANONICAL_JSON = REPO_ROOT / "__unified_v2_test__no_canonical_cutoff.json"


def _minimal_bets_int_bet_id():
    now = pd.Timestamp("2026-03-01 12:00:00")
    return pd.DataFrame(
        {
            "bet_id": [1],
            "session_id": [11],
            "player_id": [1001],
            "table_id": [1005],
            "payout_complete_dtm": [now],
            "wager": [100.0],
            "status": ["LOSE"],
            "payout_odds": [1.9],
            "base_ha": [0.02],
            "is_back_bet": [0],
            "position_idx": [0],
        }
    )


def _minimal_sessions():
    now = pd.Timestamp("2026-03-01 12:00:00")
    return pd.DataFrame(
        {
            "session_id": [11],
            "player_id": [1001],
            "session_start_dtm": [now - pd.Timedelta(hours=1)],
            "session_end_dtm": [now - pd.Timedelta(minutes=20)],
            "lud_dtm": [now - pd.Timedelta(minutes=20)],
            "is_manual": [0],
            "is_deleted": [0],
            "is_canceled": [0],
            "turnover": [1000],
            "num_games_with_wager": [5],
        }
    )


# ---------------------------------------------------------------------------
# Review #1: bet_id astype(str) asymmetry (int vs float) — MRE of intersection bug
# ---------------------------------------------------------------------------


class TestUnifiedV2BetIdStrAsymmetryMRE(unittest.TestCase):
    """Risk #1: same logical bet_id as int in new_bets vs float in features → str mismatch."""

    def test_mre_int_vs_float_bet_id_stringification_mismatch(self):
        new_bets = pd.DataFrame({"bet_id": [1]})  # int64 → str "1"
        features_all = pd.DataFrame({"bet_id": [1.0], "canonical_id": ["c1"], "player_id": [1]})
        new_ids = set(new_bets["bet_id"].astype(str))
        matched = features_all[features_all["bet_id"].astype(str).isin(new_ids)]
        self.assertTrue(
            matched.empty,
            "MRE: current score_once-style join yields 0 rows when bet_id is 1 vs 1.0",
        )
        self.assertEqual(new_ids, {"1"})
        self.assertEqual(set(features_all["bet_id"].astype(str)), {"1.0"})

    def test_when_both_sides_same_numeric_str_join_succeeds(self):
        new_bets = pd.DataFrame({"bet_id": [1.0]})
        features_all = pd.DataFrame({"bet_id": [1.0], "canonical_id": ["c1"], "player_id": [1]})
        new_ids = set(new_bets["bet_id"].astype(str))
        matched = features_all[features_all["bet_id"].astype(str).isin(new_ids)]
        self.assertEqual(len(matched), 1)


# ---------------------------------------------------------------------------
# Review #1 (integration): score_once hits "No usable rows" with int/float mismatch
# ---------------------------------------------------------------------------


class TestUnifiedV2ScoreOnceBetIdMismatchIntegration(unittest.TestCase):
    """Risk #1 integration: mismatched bet_id repr → no scoring row despite rated feature row."""

    def test_score_once_no_usable_rows_when_bet_id_int_vs_float(self):
        bets = _minimal_bets_int_bet_id()
        sessions = _minimal_sessions()
        artifacts = {
            "feature_list": ["wager"],
            "model_version": "test-v0",
            "feature_spec": None,
        }
        features_row = pd.DataFrame(
            {
                "bet_id": [1.0],
                "player_id": [1001],
                "wager": [100.0],
                "canonical_id": ["c1"],
            }
        )
        with (
            patch.object(scorer_mod, "CANONICAL_MAPPING_PARQUET", _FAKE_CANONICAL_PARQUET),
            patch.object(scorer_mod, "CANONICAL_MAPPING_CUTOFF_JSON", _FAKE_CANONICAL_JSON),
            patch.object(scorer_mod, "fetch_recent_data", return_value=(bets, sessions)),
            patch.object(scorer_mod, "normalize_bets_sessions", side_effect=lambda b, s: (b, s)),
            patch.object(scorer_mod, "prune_old_state"),
            patch.object(scorer_mod, "refresh_alert_history"),
            patch.object(scorer_mod, "update_state_with_new_bets", return_value=bets.copy()),
            patch.object(
                scorer_mod,
                "build_canonical_mapping_from_df",
                return_value=pd.DataFrame({"player_id": [1001], "canonical_id": ["c1"]}),
            ),
            patch.object(scorer_mod, "build_features_for_scoring", return_value=features_row),
            patch.object(scorer_mod, "compute_track_llm_features", side_effect=lambda df, **_: df),
            patch.object(scorer_mod, "_compute_reason_codes", return_value=["[]"]),
            patch.object(scorer_mod, "get_session_totals", return_value=(0, 0.0, None, None)),
            patch.object(scorer_mod, "get_session_count", return_value=0),
            patch.object(scorer_mod, "get_historical_avg", return_value=0.0),
            patch.object(scorer_mod, "append_alerts"),
        ):
            conn = sqlite3.connect(":memory:")
            with self.assertLogs("trainer.serving.scorer", level="INFO") as cm:
                scorer_mod.score_once(
                    artifacts,
                    lookback_hours=1,
                    alert_history=set(),
                    conn=conn,
                    retention_hours=1,
                )
        joined = " ".join(cm.output)
        self.assertIn("No usable rows after feature engineering", joined)


# ---------------------------------------------------------------------------
# Review #2: Track LLM shrinks rows — generic warning exists, no rated-new-specific msg
# ---------------------------------------------------------------------------


class TestUnifiedV2TrackLlmRowDropObservability(unittest.TestCase):
    """Risk #2: LLM can drop rows after rated slice; no dedicated 'rated new-bet' warning yet."""

    def test_track_llm_drop_logs_generic_warning_not_rated_new_bet_specific(self):
        bets = _minimal_bets_int_bet_id()
        sessions = _minimal_sessions()
        artifacts = {
            "feature_list": ["wager"],
            "model_version": "test-v0",
            "feature_spec": object(),
        }
        features_row = pd.DataFrame(
            {
                "bet_id": [1],
                "player_id": [1001],
                "wager": [100.0],
                "canonical_id": ["c1"],
            }
        )

        def _shrink_llm(df, **_kwargs):
            return df.iloc[0:0].copy()

        with (
            patch.object(scorer_mod, "CANONICAL_MAPPING_PARQUET", _FAKE_CANONICAL_PARQUET),
            patch.object(scorer_mod, "CANONICAL_MAPPING_CUTOFF_JSON", _FAKE_CANONICAL_JSON),
            patch.object(scorer_mod, "fetch_recent_data", return_value=(bets, sessions)),
            patch.object(scorer_mod, "normalize_bets_sessions", side_effect=lambda b, s: (b, s)),
            patch.object(scorer_mod, "prune_old_state"),
            patch.object(scorer_mod, "refresh_alert_history"),
            patch.object(scorer_mod, "update_state_with_new_bets", return_value=bets.copy()),
            patch.object(
                scorer_mod,
                "build_canonical_mapping_from_df",
                return_value=pd.DataFrame({"player_id": [1001], "canonical_id": ["c1"]}),
            ),
            patch.object(scorer_mod, "build_features_for_scoring", return_value=features_row),
            patch.object(scorer_mod, "compute_track_llm_features", side_effect=_shrink_llm),
            patch.object(scorer_mod, "_compute_reason_codes", return_value=["[]"]),
            patch.object(scorer_mod, "get_session_totals", return_value=(0, 0.0, None, None)),
            patch.object(scorer_mod, "get_session_count", return_value=0),
            patch.object(scorer_mod, "get_historical_avg", return_value=0.0),
            patch.object(scorer_mod, "append_alerts"),
        ):
            conn = sqlite3.connect(":memory:")
            with self.assertLogs("trainer.serving.scorer", level="WARNING") as cm:
                logging.getLogger("trainer.serving.scorer").setLevel(logging.WARNING)
                scorer_mod.score_once(
                    artifacts,
                    lookback_hours=1,
                    alert_history=set(),
                    conn=conn,
                    retention_hours=1,
                )
        joined = " ".join(cm.output)
        self.assertIn("Track LLM dropped", joined)
        self.assertNotIn(
            "rated new-bet",
            joined.lower(),
            "Contract: dedicated rated-new-bet shrink log not yet implemented (STATUS review #2)",
        )


# ---------------------------------------------------------------------------
# Review #3: missing player_id when n_unrated > 0 → KeyError
# ---------------------------------------------------------------------------


class TestUnifiedV2TelemetryMissingPlayerId(unittest.TestCase):
    """Risk #3: telemetry uses player_id for unrated branch — missing column raises."""

    def test_score_once_keyerror_when_unrated_new_rows_without_player_id(self):
        bets = _minimal_bets_int_bet_id()
        sessions = _minimal_sessions()
        artifacts = {
            "feature_list": ["wager"],
            "model_version": "test-v0",
            "feature_spec": None,
        }
        # canonical_id not in rated set → unrated; no player_id column
        features_row = pd.DataFrame(
            {
                "bet_id": [1],
                "wager": [100.0],
                "canonical_id": ["unrated_only"],
            }
        )
        with (
            patch.object(scorer_mod, "CANONICAL_MAPPING_PARQUET", _FAKE_CANONICAL_PARQUET),
            patch.object(scorer_mod, "CANONICAL_MAPPING_CUTOFF_JSON", _FAKE_CANONICAL_JSON),
            patch.object(scorer_mod, "fetch_recent_data", return_value=(bets, sessions)),
            patch.object(scorer_mod, "normalize_bets_sessions", side_effect=lambda b, s: (b, s)),
            patch.object(scorer_mod, "prune_old_state"),
            patch.object(scorer_mod, "refresh_alert_history"),
            patch.object(scorer_mod, "update_state_with_new_bets", return_value=bets.copy()),
            patch.object(
                scorer_mod,
                "build_canonical_mapping_from_df",
                return_value=pd.DataFrame({"player_id": [1001], "canonical_id": ["c1"]}),
            ),
            patch.object(scorer_mod, "build_features_for_scoring", return_value=features_row),
        ):
            conn = sqlite3.connect(":memory:")
            with self.assertRaises(KeyError):
                scorer_mod.score_once(
                    artifacts,
                    lookback_hours=1,
                    alert_history=set(),
                    conn=conn,
                    retention_hours=1,
                )


# ---------------------------------------------------------------------------
# Review #4: canonical_id dtype vs rated_canonical_ids set — isin false positive empty
# ---------------------------------------------------------------------------


class TestUnifiedV2CanonicalIdTypeParity(unittest.TestCase):
    """Risk #4: str set vs int column → rated-only slice drops rows (MRE)."""

    def test_mre_str_rated_set_vs_int_canonical_column_drops_row(self):
        rated_canonical_ids = {"1"}
        features_all = pd.DataFrame({"canonical_id": [1], "bet_id": [10]})
        sliced = features_all[features_all["canonical_id"].isin(rated_canonical_ids)]
        self.assertTrue(
            sliced.empty,
            "MRE: isin fails when canonical_id is int 1 but set holds str '1'",
        )


# ---------------------------------------------------------------------------
# Review #5: backtester logs only model_default to MLflow when optuna present
# ---------------------------------------------------------------------------


class TestUnifiedV2BacktesterMlflowOptunaGap(unittest.TestCase):
    """Risk #5: results contain optuna section but only model_default is passed to log_metrics_safe."""

    def test_log_metrics_safe_called_once_with_model_default_only(self):
        calls: list[dict] = []

        def _capture(m):
            calls.append(dict(m))

        results = {
            "model_default": {"test_ap": 0.25, "threshold": 0.5},
            "optuna": {"test_ap": 0.99, "threshold": 0.4},
        }
        with (
            patch.object(backtester_mod, "has_active_run", return_value=True),
            patch.object(backtester_mod, "log_metrics_safe", side_effect=_capture),
        ):
            if backtester_mod.has_active_run():
                _md = results.get("model_default")
                if isinstance(_md, dict):
                    backtester_mod.log_metrics_safe(
                        backtester_mod._flat_section_to_mlflow_metrics(_md)
                    )

        self.assertEqual(len(calls), 1)
        self.assertIn("backtest_ap", calls[0])
        self.assertAlmostEqual(calls[0]["backtest_ap"], 0.25)
        self.assertAlmostEqual(calls[0]["backtest_threshold"], 0.5)


# ---------------------------------------------------------------------------
# Review #6: backtester documents ImportError fallback for mlflow_utils
# ---------------------------------------------------------------------------


class TestUnifiedV2BacktesterMlflowImportContract(unittest.TestCase):
    """Risk #6: static contract — ImportError path exists (silent no-op until improved)."""

    def test_backtester_source_has_importerror_fallback_for_mlflow_utils(self):
        text = BACKTESTER_SRC.read_text(encoding="utf-8")
        self.assertIn("from trainer.core.mlflow_utils import", text)
        self.assertIn("except ImportError", text)
        self.assertIn("has_active_run", text)
        self.assertIn("log_metrics_safe", text)


# ---------------------------------------------------------------------------
# Review #7: _flat_section_to_mlflow_metrics + log_metrics_safe skips bad scalars
# ---------------------------------------------------------------------------


class TestUnifiedV2FlatMetricsNonNumeric(unittest.TestCase):
    """Risk #7: non-numeric test_ap must not break log_metrics_safe."""

    def test_flat_mapper_passes_string_ap_and_log_metrics_safe_does_not_raise(self):
        from trainer.core import mlflow_utils

        bad_flat = {"test_ap": "not_a_number", "threshold": 0.5}
        mapped = backtester_mod._flat_section_to_mlflow_metrics(bad_flat)
        self.assertIn("backtest_threshold", mapped)
        with patch.object(mlflow_utils, "is_mlflow_available", return_value=False):
            mlflow_utils.log_metrics_safe(mapped)


# ---------------------------------------------------------------------------
# Review #8: static ordering — rated-only slice before Track LLM in scorer; full-bets LLM in backtester
# ---------------------------------------------------------------------------


class TestUnifiedV8TrainServeLlmOrderingContract(unittest.TestCase):
    """Risk #8: contract that scorer slices before LLM; backtester runs LLM on full bets DataFrame."""

    def test_scorer_rated_slice_line_before_compute_track_llm_in_score_once(self):
        src = SCORER_SRC.read_text(encoding="utf-8")
        marker_slice = '.isin(rated_canonical_ids)].copy()'
        marker_llm = "compute_track_llm_features("
        pos_slice = src.find(marker_slice)
        pos_llm = src.find(marker_llm, pos_slice)
        self.assertGreater(pos_slice, 0, "rated-only slice pattern not found")
        self.assertGreater(pos_llm, pos_slice, "compute_track_llm_features should follow rated slice")

    def test_backtester_calls_track_llm_on_bets_variable(self):
        text = BACKTESTER_SRC.read_text(encoding="utf-8")
        self.assertIn("compute_track_llm_features(", text)
        self.assertIn("Track LLM on FULL bets", text)


if __name__ == "__main__":
    unittest.main()
