"""
Phase 2 T6: Integration tests for prediction log retention cleanup.

- Cleanup only deletes rows with prediction_id <= watermark and scored_at < retention_cutoff.
- Unexported rows (prediction_id > watermark) are never deleted.
- Code Review §2 §3 §5: minimal reproducible tests for risk points.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from trainer.serving import scorer as scorer_mod
from trainer.scripts.export_predictions_to_mlflow import (
    _ensure_export_meta_tables,
    _run_retention_cleanup,
    _set_last_exported_id,
)


class TestPredictionRetention(unittest.TestCase):
    """T6: Retention cleanup only removes exported rows older than cutoff."""

    def test_cleanup_deletes_only_exported_and_old_rows(self):
        """Rows with prediction_id <= watermark and scored_at < cutoff are deleted."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prediction_log.db"
            path.parent.mkdir(parents=True, exist_ok=True)
            # Two rows, both old
            import pandas as pd
            for i, st in enumerate(["2026-01-01T10:00:00+08:00", "2026-01-01T11:00:00+08:00"]):
                scorer_mod._append_prediction_log(
                    str(path),
                    st,
                    "v1",
                    pd.DataFrame([
                        {"bet_id": f"B{i}", "session_id": "S1", "player_id": "P1", "canonical_id": "C1",
                         "casino_player_id": "CP1", "table_id": "T1", "score": 0.5, "margin": 0.0, "is_rated_obs": 1},
                    ]),
                )
            conn = sqlite3.connect(str(path))
            try:
                _ensure_export_meta_tables(conn)
                _set_last_exported_id(conn, 2)  # both "exported"
                conn.commit()
                # Cutoff: 2026-01-15 so both rows are before it
                deleted = _run_retention_cleanup(conn, 2, "2026-01-15T00:00:00+08:00", 5000)
                conn.commit()
            finally:
                conn.close()
            self.assertEqual(deleted, 2)
            conn = sqlite3.connect(str(path))
            try:
                n = conn.execute("SELECT COUNT(*) FROM prediction_log").fetchone()[0]
                self.assertEqual(n, 0)
            finally:
                conn.close()

    def test_cleanup_does_not_delete_unexported_rows(self):
        """Rows with prediction_id > watermark are not deleted."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prediction_log.db"
            path.parent.mkdir(parents=True, exist_ok=True)
            import pandas as pd
            scorer_mod._append_prediction_log(
                str(path),
                "2026-01-01T10:00:00+08:00",
                "v1",
                pd.DataFrame([
                    {"bet_id": "B1", "session_id": "S1", "player_id": "P1", "canonical_id": "C1",
                     "casino_player_id": "CP1", "table_id": "T1", "score": 0.5, "margin": 0.0, "is_rated_obs": 1},
                ]),
            )
            conn = sqlite3.connect(str(path))
            try:
                _ensure_export_meta_tables(conn)
                _set_last_exported_id(conn, 0)  # nothing "exported" yet
                conn.commit()
                # Cutoff in future so any row would be "old" by time, but watermark=0 so we delete prediction_id <= 0 (none)
                deleted = _run_retention_cleanup(conn, 0, "2026-01-15T00:00:00+08:00", 5000)
                conn.commit()
            finally:
                conn.close()
            self.assertEqual(deleted, 0)
            conn = sqlite3.connect(str(path))
            try:
                n = conn.execute("SELECT COUNT(*) FROM prediction_log").fetchone()[0]
                self.assertEqual(n, 1)
            finally:
                conn.close()

    def test_retention_cleanup_with_batch_size_zero_returns_zero_and_deletes_nothing(self):
        """T6 Review §3: batch_size=0 => LIMIT 0, no rows deleted; documents current no-op behavior."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prediction_log.db"
            path.parent.mkdir(parents=True, exist_ok=True)
            import pandas as pd
            scorer_mod._append_prediction_log(
                str(path),
                "2026-01-01T10:00:00+08:00",
                "v1",
                pd.DataFrame([
                    {"bet_id": "B1", "session_id": "S1", "player_id": "P1", "canonical_id": "C1",
                     "casino_player_id": "CP1", "table_id": "T1", "score": 0.5, "margin": 0.0, "is_rated_obs": 1},
                ]),
            )
            conn = sqlite3.connect(str(path))
            try:
                _ensure_export_meta_tables(conn)
                _set_last_exported_id(conn, 1)
                conn.commit()
                deleted = _run_retention_cleanup(conn, 1, "2026-01-15T00:00:00+08:00", 0)
                conn.commit()
            finally:
                conn.close()
            self.assertEqual(deleted, 0)
            conn = sqlite3.connect(str(path))
            try:
                n = conn.execute("SELECT COUNT(*) FROM prediction_log").fetchone()[0]
                self.assertEqual(n, 1)
            finally:
                conn.close()

    def test_retention_cleanup_with_future_cutoff_deletes_all_exported_rows(self):
        """T6 Review §2 §4: When cutoff is in the future (e.g. retention_days=-1), scored_at < cutoff for all rows => all exported rows deleted. Documents current behavior."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prediction_log.db"
            path.parent.mkdir(parents=True, exist_ok=True)
            import pandas as pd
            scorer_mod._append_prediction_log(
                str(path),
                "2026-01-01T10:00:00+08:00",
                "v1",
                pd.DataFrame([
                    {"bet_id": "B1", "session_id": "S1", "player_id": "P1", "canonical_id": "C1",
                     "casino_player_id": "CP1", "table_id": "T1", "score": 0.5, "margin": 0.0, "is_rated_obs": 1},
                ]),
            )
            conn = sqlite3.connect(str(path))
            try:
                _ensure_export_meta_tables(conn)
                _set_last_exported_id(conn, 1)
                conn.commit()
                future_cutoff = "2030-01-01T00:00:00+08:00"
                deleted = _run_retention_cleanup(conn, 1, future_cutoff, 5000)
                conn.commit()
            finally:
                conn.close()
            self.assertEqual(deleted, 1)
            conn = sqlite3.connect(str(path))
            try:
                n = conn.execute("SELECT COUNT(*) FROM prediction_log").fetchone()[0]
                self.assertEqual(n, 0)
            finally:
                conn.close()

    def test_retention_cleanup_with_large_batch_size_completes(self):
        """T6 Review §5: Large batch_size does not crash; still deletes correctly."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prediction_log.db"
            path.parent.mkdir(parents=True, exist_ok=True)
            import pandas as pd
            scorer_mod._append_prediction_log(
                str(path),
                "2026-01-01T10:00:00+08:00",
                "v1",
                pd.DataFrame([
                    {"bet_id": "B1", "session_id": "S1", "player_id": "P1", "canonical_id": "C1",
                     "casino_player_id": "CP1", "table_id": "T1", "score": 0.5, "margin": 0.0, "is_rated_obs": 1},
                ]),
            )
            conn = sqlite3.connect(str(path))
            try:
                _ensure_export_meta_tables(conn)
                _set_last_exported_id(conn, 1)
                conn.commit()
                deleted = _run_retention_cleanup(conn, 1, "2026-01-15T00:00:00+08:00", 100_000)
                conn.commit()
            finally:
                conn.close()
            self.assertEqual(deleted, 1)
            conn = sqlite3.connect(str(path))
            try:
                n = conn.execute("SELECT COUNT(*) FROM prediction_log").fetchone()[0]
                self.assertEqual(n, 0)
            finally:
                conn.close()
