"""
Phase 2 T4: Contract tests for prediction_log schema (PLAN P1.1).

- prediction_log table has required columns per PLAN T4.
- Scorer exposes _ensure_prediction_log_table and _append_prediction_log.
- Code Review T4 §1: score_once passes only the DataFrame from _score_df to _append_prediction_log.
"""

from __future__ import annotations

import inspect
import sqlite3
import tempfile
import unittest
from pathlib import Path

from trainer.serving import scorer as scorer_mod


def _score_once_src() -> str:
    return inspect.getsource(scorer_mod.score_once)


class TestPredictionLogSchema(unittest.TestCase):
    """Phase 2 T4: prediction_log table must have PLAN-defined columns."""

    _REQUIRED_COLUMNS = [
        "prediction_id",
        "scored_at",
        "bet_id",
        "session_id",
        "player_id",
        "canonical_id",
        "casino_player_id",
        "table_id",
        "model_version",
        "score",
        "margin",
        "is_alert",
        "is_rated_obs",
    ]

    def test_prediction_log_table_has_required_columns(self):
        """Creating the table must produce all PLAN T4 columns."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prediction_log.db"
            conn = sqlite3.connect(str(path))
            try:
                conn.execute("PRAGMA journal_mode=WAL;")
                scorer_mod._ensure_prediction_log_table(conn)
                conn.commit()
                cursor = conn.execute("PRAGMA table_info(prediction_log)")
                cols = {row[1] for row in cursor.fetchall()}
                for col in self._REQUIRED_COLUMNS:
                    self.assertIn(col, cols, f"prediction_log must have column {col!r} (PLAN T4)")
            finally:
                conn.close()


class TestScoreOncePassesFeaturesDfToAppendPredictionLog(unittest.TestCase):
    """Code Review T4 §1: score_once must pass only the DataFrame from _score_df to _append_prediction_log."""

    def test_append_prediction_log_called_with_features_df_from_score_df(self):
        """_append_prediction_log is called with the variable assigned from _score_df (features_df)."""
        src = _score_once_src()
        self.assertIn("_append_prediction_log", src)
        self.assertIn("_score_df", src)
        idx_score_df = src.find("features_df = _score_df(")
        self.assertGreater(idx_score_df, 0, "score_once should assign features_df from _score_df")
        idx_append = src.find("_append_prediction_log(")
        self.assertGreater(idx_append, idx_score_df, "_append_prediction_log call should appear after features_df = _score_df")
        between = src[idx_score_df:idx_append]
        self.assertIn(
            "features_df",
            between,
            "_append_prediction_log should be called with features_df (DataFrame from _score_df, which has score, margin, is_rated_obs)",
        )
