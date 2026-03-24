"""
Phase 2 T5: Integration tests for prediction export (watermark, batch, dry-run).

- Export script creates meta tables when DB exists; no rows -> return 0.
- Dry-run with batch does not advance watermark.
- Code Review §2 §4 §5 §6 §8: minimal reproducible tests for risk points.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
import pandas as pd

from trainer.serving import scorer as scorer_mod
from trainer.scripts import export_predictions_to_mlflow as export_mod
from trainer.scripts.export_predictions_to_mlflow import (
    _ensure_export_meta_tables,
    _get_last_exported_id,
    run_export,
)


class TestExportWatermark(unittest.TestCase):
    """T5: Export script watermark and dry-run behavior."""

    def test_export_empty_db_returns_zero(self):
        """DB exists but no prediction_log rows; run_export returns 0."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prediction_log.db"
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(path))
            try:
                conn.execute("PRAGMA journal_mode=WAL;")
                _ensure_export_meta_tables(conn)
                conn.commit()
            finally:
                conn.close()
            rc = run_export(
                db_path=str(path),
                safety_lag_minutes=5,
                batch_rows=100,
                dry_run=False,
            )
            self.assertEqual(rc, 0)

    def test_export_dry_run_does_not_advance_watermark(self):
        """With rows in prediction_log, dry_run=True returns 0 and watermark stays 0."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prediction_log.db"
            path.parent.mkdir(parents=True, exist_ok=True)
            # Use scorer to create prediction_log and append one row (creates meta too)
            import pandas as pd
            df = pd.DataFrame([
                {
                    "bet_id": "B1",
                    "session_id": "S1",
                    "player_id": "P1",
                    "canonical_id": "C1",
                    "casino_player_id": "CP1",
                    "table_id": "T1",
                    "score": 0.5,
                    "margin": 0.0,
                    "is_rated_obs": 1,
                },
            ])
            scorer_mod._append_prediction_log(
                str(path),
                "2026-03-18T10:00:00+08:00",  # old enough for safety_lag
                "v1",
                df,
            )
            conn = sqlite3.connect(str(path))
            try:
                before = _get_last_exported_id(conn)
            finally:
                conn.close()
            self.assertEqual(before, 0)

            rc = run_export(
                db_path=str(path),
                safety_lag_minutes=5,
                batch_rows=100,
                dry_run=True,
            )
            self.assertEqual(rc, 0)

            conn = sqlite3.connect(str(path))
            try:
                after = _get_last_exported_id(conn)
            finally:
                conn.close()
            self.assertEqual(after, 0, "dry-run must not advance watermark")

    def test_upload_success_watermark_update_failure_does_not_advance_watermark(self):
        """Review §2: When _set_last_exported_id raises after upload, watermark stays 0."""
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
                    "score": 0.5,
                    "margin": 0.0,
                    "is_rated_obs": 1,
                },
            ])
            scorer_mod._append_prediction_log(
                str(path),
                "2026-03-18T10:00:00+08:00",
                "v1",
                df,
            )
            mock_mlflow = MagicMock()
            # Avoid to_parquet/pyarrow in test env; just write a minimal file so upload path runs
            def _fake_to_parquet(self, path, **kwargs):
                Path(path).write_bytes(b"\0")
            with patch.object(export_mod, "is_mlflow_available", return_value=True), \
                 patch.dict(sys.modules, {"mlflow": mock_mlflow}), \
                 patch.object(pd.DataFrame, "to_parquet", _fake_to_parquet), \
                 patch.object(export_mod, "_set_last_exported_id", side_effect=Exception("simulated failure")):
                with self.assertRaises(Exception) as ctx:
                    run_export(
                        db_path=str(path),
                        safety_lag_minutes=5,
                        batch_rows=100,
                        dry_run=False,
                    )
                self.assertIn("simulated failure", str(ctx.exception))
            conn = sqlite3.connect(str(path))
            try:
                self.assertEqual(_get_last_exported_id(conn), 0)
            finally:
                conn.close()

    def test_run_export_with_large_batch_rows_completes(self):
        """Review §4: run_export with very large batch_rows does not crash (no OOM in test)."""
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
                    "score": 0.5,
                    "margin": 0.0,
                    "is_rated_obs": 1,
                },
            ])
            scorer_mod._append_prediction_log(
                str(path),
                "2026-03-18T10:00:00+08:00",
                "v1",
                df,
            )
            rc = run_export(
                db_path=str(path),
                safety_lag_minutes=5,
                batch_rows=2_000_000,
                dry_run=True,
            )
            self.assertEqual(rc, 0)

    def test_scored_at_cutoff_boundary_only_exports_rows_at_or_before_cutoff(self):
        """Review §5: Only rows with scored_at <= cutoff are in the batch (ISO HK string compare)."""
        cutoff_ts = "2026-03-18T12:00:00+08:00"
        after_ts = "2026-03-18T12:00:01+08:00"
        fixed_now = datetime.fromisoformat(cutoff_ts)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prediction_log.db"
            path.parent.mkdir(parents=True, exist_ok=True)
            scorer_mod._append_prediction_log(str(path), cutoff_ts, "v1", pd.DataFrame([
                {"bet_id": "B1", "session_id": "S1", "player_id": "P1", "canonical_id": "C1",
                 "casino_player_id": "CP1", "table_id": "T1", "score": 0.5, "margin": 0.0, "is_rated_obs": 1},
            ]))
            conn = sqlite3.connect(str(path))
            try:
                conn.execute(
                    """INSERT INTO prediction_log (
                        scored_at, bet_id, session_id, player_id, canonical_id,
                        casino_player_id, table_id, model_version, score, margin, is_alert, is_rated_obs
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (after_ts, "B2", "S2", "P2", "C2", "CP2", "T2", "v1", 0.4, 0.0, 0, 1),
                )
                conn.commit()
            finally:
                conn.close()
            mock_mlflow = MagicMock()
            with patch("trainer.scripts.export_predictions_to_mlflow.datetime") as mock_dt, \
                 patch.object(export_mod, "is_mlflow_available", return_value=True), \
                 patch.dict(sys.modules, {"mlflow": mock_mlflow}):
                mock_dt.now.return_value = fixed_now
                rc = run_export(
                    db_path=str(path),
                    safety_lag_minutes=0,
                    batch_rows=100,
                    dry_run=False,
                )
                self.assertEqual(rc, 0)
            conn = sqlite3.connect(str(path))
            try:
                last_id = _get_last_exported_id(conn)
                runs = conn.execute(
                    "SELECT row_count, min_prediction_id, max_prediction_id FROM prediction_export_runs ORDER BY id DESC LIMIT 1"
                ).fetchone()
            finally:
                conn.close()
            self.assertEqual(last_id, 1, "only first row (prediction_id=1) in batch")
            self.assertIsNotNone(runs)
            self.assertEqual(runs[0], 1)
            self.assertEqual(runs[1], 1)
            self.assertEqual(runs[2], 1)

    def test_get_last_exported_id_when_value_null_raises_type_error(self):
        """Review §6: When meta row exists but value is None, _get_last_exported_id raises TypeError (current behavior)."""
        from unittest.mock import MagicMock
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = (None,)
        mock_conn.execute.return_value = mock_cursor
        with self.assertRaises(TypeError):
            _get_last_exported_id(mock_conn)

    def test_export_meta_schema_matches_scorer_and_script(self):
        """Review §8: prediction_export_meta and prediction_export_runs schema from scorer and export script match."""
        def table_info(conn: sqlite3.Connection, name: str) -> list[tuple]:
            return conn.execute(f"PRAGMA table_info({name})").fetchall()

        with tempfile.TemporaryDirectory() as tmp1, tempfile.TemporaryDirectory() as tmp2:
            path1 = Path(tmp1) / "p1.db"
            path2 = Path(tmp2) / "p2.db"
            path1.parent.mkdir(parents=True, exist_ok=True)
            path2.parent.mkdir(parents=True, exist_ok=True)
            conn1 = sqlite3.connect(str(path1))
            conn2 = sqlite3.connect(str(path2))
            try:
                conn1.execute("PRAGMA journal_mode=WAL;")
                conn2.execute("PRAGMA journal_mode=WAL;")
                scorer_mod._ensure_prediction_log_table(conn1)
                _ensure_export_meta_tables(conn2)
                conn1.commit()
                conn2.commit()
            finally:
                conn1.close()
                conn2.close()
            conn1 = sqlite3.connect(str(path1))
            conn2 = sqlite3.connect(str(path2))
            try:
                meta1 = table_info(conn1, "prediction_export_meta")
                meta2 = table_info(conn2, "prediction_export_meta")
                runs1 = table_info(conn1, "prediction_export_runs")
                runs2 = table_info(conn2, "prediction_export_runs")
            finally:
                conn1.close()
                conn2.close()
            self.assertEqual([(r[1], r[2]) for r in meta1], [(r[1], r[2]) for r in meta2])
            self.assertEqual([(r[1], r[2]) for r in runs1], [(r[1], r[2]) for r in runs2])
