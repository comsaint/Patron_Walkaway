"""
Phase 2 T4: Integration tests for prediction_log SQLite write path.

- _append_prediction_log creates table and inserts rows; query returns them.
- Scorer does not crash when PREDICTION_LOG_DB_PATH is set and score path runs.
"""

from __future__ import annotations

import unittest
from pathlib import Path

import pandas as pd

from trainer.serving import scorer as scorer_mod


class TestAppendPredictionLog(unittest.TestCase):
    """Phase 2 T4: _append_prediction_log writes rows to prediction_log."""

    def test_append_prediction_log_creates_table_and_inserts_rows(self):
        """With a temp DB, _append_prediction_log creates table and rows are readable."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prediction_log.db"
            path.parent.mkdir(parents=True, exist_ok=True)
            df = pd.DataFrame([
                {
                    "bet_id": "B1",
                    "session_id": "S1",
                    "player_id": "P1",
                    "canonical_id": "C1",
                    "casino_player_id": "CP1",
                    "table_id": "T1",
                    "score": 0.6,
                    "margin": 0.1,
                    "is_rated_obs": 1,
                },
            ])
            scorer_mod._append_prediction_log(
                str(path),
                "2026-03-18T12:00:00+08:00",
                "20260318-120000-abc1234",
                df,
            )
            import sqlite3
            conn = sqlite3.connect(str(path))
            try:
                rows = conn.execute(
                    "SELECT prediction_id, scored_at, bet_id, model_version, score, margin, is_alert, is_rated_obs FROM prediction_log"
                ).fetchall()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0][2], "B1")
                self.assertEqual(rows[0][3], "20260318-120000-abc1234")
                self.assertEqual(rows[0][4], 0.6)
                self.assertEqual(rows[0][6], 1)  # is_alert (margin >= 0 and is_rated_obs)
                self.assertEqual(rows[0][7], 1)  # is_rated_obs
            finally:
                conn.close()

    def test_append_prediction_log_raises_when_missing_required_column(self):
        """Code Review T4 §1: df missing required column (e.g. score) raises KeyError (current behavior)."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prediction_log.db"
            path.parent.mkdir(parents=True, exist_ok=True)
            df = pd.DataFrame([
                {
                    "bet_id": "B1",
                    "session_id": "S1",
                    "player_id": "P1",
                    "canonical_id": "C1",
                    "casino_player_id": "CP1",
                    "table_id": "T1",
                    "margin": 0.1,
                    "is_rated_obs": 1,
                },
            ])
            with self.assertRaises(KeyError):
                scorer_mod._append_prediction_log(
                    str(path),
                    "2026-03-18T12:00:00+08:00",
                    "20260318-120000-abc1234",
                    df,
                )

    def test_append_prediction_log_nan_score_current_behavior(self):
        """Code Review T4 §4: One row with score=nan — current behavior is IntegrityError (NOT NULL).
        When production coerces NaN to NULL, this test can be relaxed to assert 1 row written."""
        import sqlite3
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prediction_log.db"
            path.parent.mkdir(parents=True, exist_ok=True)
            df = pd.DataFrame([
                {
                    "bet_id": "B2",
                    "session_id": "S2",
                    "player_id": "P2",
                    "canonical_id": "C2",
                    "casino_player_id": "CP2",
                    "table_id": "T2",
                    "score": float("nan"),
                    "margin": 0.0,
                    "is_rated_obs": 0,
                },
            ])
            with self.assertRaises((sqlite3.IntegrityError, TypeError)):
                scorer_mod._append_prediction_log(
                    str(path),
                    "2026-03-18T12:00:00+08:00",
                    "20260318-120000-abc1234",
                    df,
                )

    def test_append_prediction_log_closes_connection_on_commit_failure(self):
        """Code Review T4 §5: When commit() raises, conn.close() is still called."""
        from unittest.mock import MagicMock, patch
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prediction_log.db"
            path.parent.mkdir(parents=True, exist_ok=True)
            df = pd.DataFrame([
                {
                    "bet_id": "B3",
                    "session_id": "S3",
                    "player_id": "P3",
                    "canonical_id": "C3",
                    "casino_player_id": "CP3",
                    "table_id": "T3",
                    "score": 0.5,
                    "margin": 0.0,
                    "is_rated_obs": 1,
                },
            ])
            mock_conn = MagicMock()
            mock_conn.execute.return_value = None
            mock_conn.executemany.return_value = None
            mock_conn.commit.side_effect = Exception("simulated commit failure")
            with patch.object(scorer_mod, "sqlite3") as mock_sqlite3:
                mock_sqlite3.connect.return_value = mock_conn
                try:
                    scorer_mod._append_prediction_log(
                        str(path),
                        "2026-03-18T12:00:00+08:00",
                        "v1",
                        df,
                    )
                except Exception:
                    pass
            mock_conn.close.assert_called_once()

    def test_append_prediction_log_batch_1000_rows_completes_with_correct_count(self):
        """Code Review T4 §2: 1000 rows complete and DB row count is correct (no strict timeout)."""
        import tempfile
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prediction_log.db"
            path.parent.mkdir(parents=True, exist_ok=True)
            n = 1000
            df = pd.DataFrame([
                {
                    "bet_id": f"B{i}",
                    "session_id": f"S{i}",
                    "player_id": f"P{i}",
                    "canonical_id": f"C{i}",
                    "casino_player_id": f"CP{i}",
                    "table_id": f"T{i}",
                    "score": 0.5,
                    "margin": 0.0,
                    "is_rated_obs": 1,
                }
                for i in range(n)
            ])
            scorer_mod._append_prediction_log(
                str(path),
                "2026-03-18T12:00:00+08:00",
                "20260318-120000-abc1234",
                df,
            )
            conn = sqlite3.connect(str(path))
            try:
                count = conn.execute("SELECT COUNT(*) FROM prediction_log").fetchone()[0]
                self.assertEqual(count, n)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
