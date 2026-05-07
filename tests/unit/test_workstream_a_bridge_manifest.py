"""Issue #14 Workstream A: bridge manifest ingress contract tests."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as pq

from trainer.training import data_sources as ds
from trainer.training import local_bridge_preflight as lbp
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
                log = logging.getLogger("test_ws1")
                with patch(
                    "trainer.training.local_bridge_preflight._resolve_default_snap_root",
                    return_value=root / "snap_dummy",
                ):
                    with patch(
                        "parallel_lda_mvp.trainer_bridge_mvp.emit_trainer_local_parquet",
                        side_effect=_fake_emit,
                    ):
                        with self.assertLogs(log, level="INFO") as log_cm:
                            ensure_local_bridge_ready_for_training(logger=log)
                self.assertTrue(
                    any("AutoBuild[summary]" in r and "total_s=" in r for r in log_cm.output),
                    log_cm.output,
                )
                ingress = root / "trainer_local_parquet_bridge.manifest.json"
                self.assertTrue(ingress.is_file())
                self.assertTrue(ds.probe_trainer_local_parquet_bridge_readiness().ready)
            finally:
                ds.LOCAL_PARQUET_DIR = old

    def test_autobuild_full_mvp_disabled_raises(self) -> None:
        """WS2: TRAINER_AUTOBUILD_FULL_MVP=0 and no snap -> clear RuntimeError."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "parallel_lda_mvp").mkdir(parents=True, exist_ok=True)
            old = ds.LOCAL_PARQUET_DIR
            ds.LOCAL_PARQUET_DIR = root
            try:
                with patch.dict(os.environ, {"TRAINER_AUTOBUILD_FULL_MVP": "0"}, clear=False):
                    with self.assertRaises(RuntimeError) as ctx:
                        ensure_local_bridge_ready_for_training(
                            logger=logging.getLogger("test_ws2"),
                        )
                self.assertIn("AutoBuild[mvp_full]", str(ctx.exception))
                self.assertIn("disables", str(ctx.exception))
            finally:
                ds.LOCAL_PARQUET_DIR = old

    def test_autobuild_full_mvp_subprocess_then_copy(self) -> None:
        """WS2: no snap -> subprocess run_mvp; manifest copied to ingress."""

        def _fake_subprocess_run(cmd: list, **kwargs: object) -> CompletedProcess:
            dr = ds.LOCAL_PARQUET_DIR
            bridge = dr / "mvp_trainer_bridge"
            bridge.mkdir(parents=True, exist_ok=True)
            bet = dr / "bridge_bet.parquet"
            sess = dr / "bridge_sess.parquet"
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
            return CompletedProcess(cmd, 0)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "parallel_lda_mvp").mkdir(parents=True, exist_ok=True)
            old = ds.LOCAL_PARQUET_DIR
            ds.LOCAL_PARQUET_DIR = root
            try:
                log = logging.getLogger("test_ws2b")
                with patch.dict(os.environ, {"TRAINER_AUTOBUILD_FULL_MVP": "1"}, clear=False):
                    with patch.object(lbp, "_resolve_default_snap_root", return_value=None):
                        with patch.object(lbp, "_assert_l0_inputs_for_full_mvp"):
                            with patch(
                                "trainer.training.local_bridge_preflight.subprocess.run",
                                side_effect=_fake_subprocess_run,
                            ):
                                with self.assertLogs(log, level="INFO") as log_cm:
                                    ensure_local_bridge_ready_for_training(logger=log)
                self.assertTrue(
                    any(
                        "AutoBuild[summary]" in r
                        and "mvp_full_s=" in r
                        and "manifest_copy_s=" in r
                        for r in log_cm.output
                    ),
                    log_cm.output,
                )
                ing = root / "trainer_local_parquet_bridge.manifest.json"
                self.assertTrue(ing.is_file())
                self.assertTrue(ds.probe_trainer_local_parquet_bridge_readiness().ready)
            finally:
                ds.LOCAL_PARQUET_DIR = old

    def test_autobuild_full_mvp_subprocess_nonzero_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "parallel_lda_mvp").mkdir(parents=True, exist_ok=True)
            old = ds.LOCAL_PARQUET_DIR
            ds.LOCAL_PARQUET_DIR = root
            try:
                with patch.object(lbp, "_resolve_default_snap_root", return_value=None):
                    with patch.object(lbp, "_assert_l0_inputs_for_full_mvp"):
                        with patch(
                            "trainer.training.local_bridge_preflight.subprocess.run",
                            return_value=CompletedProcess(["x"], 1),
                        ):
                            with self.assertRaises(RuntimeError) as ctx:
                                ensure_local_bridge_ready_for_training(
                                    logger=logging.getLogger("test_ws2c"),
                                )
                self.assertIn("AutoBuild[mvp_full]", str(ctx.exception))
                self.assertIn("exit code=1", str(ctx.exception))
            finally:
                ds.LOCAL_PARQUET_DIR = old


if __name__ == "__main__":
    unittest.main()
