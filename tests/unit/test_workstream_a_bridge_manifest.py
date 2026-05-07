"""Issue #14 Workstream A: bridge manifest ingress contract tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from trainer.training import data_sources as ds


class TestWorkstreamABridgeManifest(unittest.TestCase):
    def test_manifest_missing_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            old = ds.LOCAL_PARQUET_DIR
            ds.LOCAL_PARQUET_DIR = root
            try:
                with self.assertRaises(FileNotFoundError) as ctx:
                    ds.load_trainer_local_parquet_bridge_manifest()
                self.assertIn("Workstream A", str(ctx.exception))
            finally:
                ds.LOCAL_PARQUET_DIR = old

    def test_phase_c_true_missing_lda_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bet = root / "gmwds_t_bet.parquet"
            sess = root / "gmwds_t_session.parquet"
            pq.write_table(pa.table({"bet_id": [1], "session_id": [1]}), bet)
            pq.write_table(pa.table({"session_id": [1]}), sess)
            (root / "trainer_local_parquet_bridge.manifest.json").write_text(
                json.dumps({
                    "artifact_kind": "trainer_local_parquet_bridge_v1",
                    "t_bet_paths": [str(bet.resolve())],
                    "gmwds_t_session": str(sess.resolve()),
                    "phase_c": True,
                }),
                encoding="utf-8",
            )
            old = ds.LOCAL_PARQUET_DIR
            ds.LOCAL_PARQUET_DIR = root
            try:
                ws = datetime(2026, 1, 1)
                ee = datetime(2026, 2, 1)
                with self.assertRaises(ValueError) as ctx:
                    ds.load_local_parquet(ws, ee)
                self.assertIn("lda_l1_run_bet_count", str(ctx.exception))
            finally:
                ds.LOCAL_PARQUET_DIR = old

    def test_resolve_prefers_t_bet_paths(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bp = root / "custom_bet.parquet"
            sp = root / "sess.parquet"
            ignored = root / "ignored_bet.parquet"
            bp.write_bytes(b"")
            sp.write_bytes(b"")
            ignored.write_bytes(b"")
            m = {
                "t_bet_paths": [str(bp.resolve())],
                "gmwds_t_bet": str(ignored.resolve()),
                "gmwds_t_session": str(sp.resolve()),
            }
            b, s = ds.resolve_local_parquet_bet_session_paths_from_manifest(m)
            self.assertEqual(b.resolve(), bp.resolve())
            self.assertEqual(s.resolve(), sp.resolve())


if __name__ == "__main__":
    unittest.main()
