"""Issue #14 Workstream A: bridge manifest ingress contract tests."""

from __future__ import annotations

import json
import logging
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as pq

from trainer.training import data_sources as ds
from trainer.training.local_bridge_preflight import ensure_local_bridge_ready_for_training


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

    def test_probe_not_ready_when_manifest_missing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            old = ds.LOCAL_PARQUET_DIR
            ds.LOCAL_PARQUET_DIR = root
            try:
                r = ds.probe_trainer_local_parquet_bridge_readiness()
                self.assertFalse(r.ready)
                self.assertTrue(any("manifest_missing" in x for x in r.reasons))
            finally:
                ds.LOCAL_PARQUET_DIR = old

    def test_probe_ready_with_phase_c_columns(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bet = root / "b.parquet"
            sess = root / "s.parquet"
            cols: dict = {"bet_id": [1], "session_id": [1]}
            for c in ds._OPTIONAL_BET_LDA_PHASE_C_COLS:
                cols[c] = [1.0]
            pq.write_table(pa.table(cols), bet)
            pq.write_table(pa.table({"session_id": [1]}), sess)
            (root / "trainer_local_parquet_bridge.manifest.json").write_text(
                json.dumps({
                    "t_bet_paths": [str(bet.resolve())],
                    "gmwds_t_session": str(sess.resolve()),
                    "phase_c": True,
                }),
                encoding="utf-8",
            )
            old = ds.LOCAL_PARQUET_DIR
            ds.LOCAL_PARQUET_DIR = root
            try:
                r = ds.probe_trainer_local_parquet_bridge_readiness()
                self.assertTrue(r.ready, r.reasons)
            finally:
                ds.LOCAL_PARQUET_DIR = old

    def test_ensure_local_bridge_mock_emit_installs_ingress_manifest(self) -> None:
        """WS1: orchestrator copies bridge manifest to trainer ingress path."""

        def _fake_emit(**kwargs: object) -> Path:
            data_dir = Path(kwargs["data_dir"])
            bridge = data_dir / "mvp_trainer_bridge"
            bridge.mkdir(parents=True, exist_ok=True)
            bet = data_dir / "bridge_bet.parquet"
            sess = data_dir / "bridge_sess.parquet"
            cols: dict = {"bet_id": [1], "session_id": [1]}
            for c in ds._OPTIONAL_BET_LDA_PHASE_C_COLS:
                cols[c] = [1.0]
            pq.write_table(pa.table(cols), bet)
            pq.write_table(pa.table({"session_id": [1]}), sess)
            mf = bridge / "trainer_local_parquet_bridge.manifest.json"
            mf.write_text(
                json.dumps({
                    "artifact_kind": "trainer_local_parquet_bridge_v1",
                    "phase_c": True,
                    "t_bet_paths": [str(bet.resolve())],
                    "gmwds_t_session": str(sess.resolve()),
                }),
                encoding="utf-8",
            )
            return mf

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            old = ds.LOCAL_PARQUET_DIR
            ds.LOCAL_PARQUET_DIR = root
            try:
                with patch(
                    "trainer.training.local_bridge_preflight._resolve_default_snap_root",
                    return_value=root / "snap_dummy",
                ):
                    with patch(
                        "parallel_lda_mvp.trainer_bridge_mvp.emit_trainer_local_parquet",
                        side_effect=_fake_emit,
                    ):
                        ensure_local_bridge_ready_for_training(
                            logger=logging.getLogger("test_ws1"),
                        )
                ingress = root / "trainer_local_parquet_bridge.manifest.json"
                self.assertTrue(ingress.is_file())
                self.assertTrue(ds.probe_trainer_local_parquet_bridge_readiness().ready)
            finally:
                ds.LOCAL_PARQUET_DIR = old


if __name__ == "__main__":
    unittest.main()
