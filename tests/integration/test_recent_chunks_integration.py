"""Integration: run_pipeline 單視窗路徑（get_single_window_chunk）與 profile 視窗 wiring.

舊版 ``--recent-chunks`` / monthly trim 已移除；本檔保留檔名以降低 CI 清單變更，
但僅驗證單一視窗 mock 下 ensure_player_profile / process_chunk 契約。
"""

from __future__ import annotations

import argparse
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from contextlib import ExitStack
from unittest.mock import ANY, MagicMock, patch

import pandas as pd
from zoneinfo import ZoneInfo

from trainer.trainer import run_pipeline

HK_TZ = ZoneInfo("Asia/Hong_Kong")


class TestSingleWindowProfileWiring(unittest.TestCase):
    def test_single_window_propagates_effective_window(self) -> None:
        base_time = datetime(2025, 1, 1, tzinfo=HK_TZ)
        end_time = base_time + timedelta(days=60)
        fake_chunks = [
            {
                "window_start": base_time,
                "window_end": end_time,
                "extended_end": end_time + timedelta(days=1),
            }
        ]
        expected_effective_start = fake_chunks[0]["window_start"].replace(tzinfo=None)
        expected_effective_end = fake_chunks[0]["window_end"].replace(tzinfo=None)
        cmap = pd.DataFrame(columns=["player_id", "canonical_id"])
        _mock_canonical_parquet = MagicMock()
        _mock_canonical_parquet.exists.return_value = False
        _mock_canonical_cutoff = MagicMock()
        _mock_canonical_cutoff.exists.return_value = False

        _fd, _tmp_parquet = tempfile.mkstemp(suffix=".parquet")
        os.close(_fd)
        _seed_df = pd.DataFrame(
            {
                "payout_complete_dtm": [datetime(2025, 2, 15, tzinfo=HK_TZ)],
                "label": [1],
                "is_rated": [True],
                "canonical_id": ["C0"],
                "bet_id": [1],
                "run_id": [1],
            }
        )
        _seed_df.to_parquet(_tmp_parquet, index=False)
        _pqt = Path(_tmp_parquet)
        mock_proc = MagicMock()
        mock_proc.return_value = _pqt
        mock_ensure = MagicMock()
        mock_load_profile = MagicMock(return_value=pd.DataFrame())
        mock_train = MagicMock(
            return_value=({"model": None, "threshold": 0.5, "features": []}, None, {})
        )
        mock_links = MagicMock(
            return_value=(
                pd.DataFrame(columns=["player_id", "casino_player_id", "lud_dtm"]),
                set(),
            )
        )
        try:
            patches = (
                patch("trainer.training.cross_entry_preflight.run_cross_entry_data_preflight"),
                patch("trainer.features.features._track_section_enabled_in_spec", return_value=True),
                patch(
                    "trainer.training.pipeline_run_core.l2_bundle_materialize.materialize_l2_training_bundle_dir",
                    return_value=Path(tempfile.mkdtemp(prefix="l2bundle_")),
                ),
                patch(
                    "trainer.training.pipeline_run_core.pipeline_l2_bundle.execute_l2_training_bundle",
                    MagicMock(return_value=None),
                ),
                patch(
                    "trainer.training.pipeline_run_core.l2_bundle_materialize.auto_bundle_cache_is_current",
                    return_value=False,
                ),
                patch("trainer.training.pipeline_run_core.STEP7_USE_DUCKDB", False),
                patch("trainer.training.pipeline_run_core.STEP7_KEEP_TRAIN_ON_DISK", False),
                patch(
                    "trainer.training.pipeline_run_core.local_parquet_session_path_for_trainer",
                    return_value=_pqt,
                ),
                patch(
                    "trainer.training.pipeline_run_core.load_local_parquet",
                    return_value=(pd.DataFrame(), pd.DataFrame()),
                ),
                patch(
                    "trainer.training.pipeline_run_core.apply_dq",
                    return_value=(pd.DataFrame(), pd.DataFrame()),
                ),
                patch("trainer.training.pipeline_run_core.CANONICAL_MAPPING_PARQUET", new=_mock_canonical_parquet),
                patch("trainer.training.pipeline_run_core.CANONICAL_MAPPING_CUTOFF_JSON", new=_mock_canonical_cutoff),
                patch("trainer.training.pipeline_run_core.build_canonical_links_and_dummy_from_duckdb", new=mock_links),
                patch("trainer.training.pipeline_run_core.build_canonical_mapping_from_links", return_value=cmap),
                patch("trainer.training.pipeline_run_core.build_canonical_mapping_from_df", return_value=cmap),
                patch("trainer.training.pipeline_run_core.get_dummy_player_ids_from_df", return_value=set()),
                patch("trainer.training.pipeline_run_core.ensure_player_profile_ready", new=mock_ensure),
                patch("trainer.training.pipeline_run_core.load_player_profile", new=mock_load_profile),
                patch("trainer.training.pipeline_run_core.process_chunk", new=mock_proc),
                patch("trainer.training.pipeline_run_core.get_single_window_chunk", return_value=fake_chunks),
                patch("trainer.training.pipeline_run_core.train_single_rated_model", new=mock_train),
                patch("trainer.training.pipeline_run_core.save_artifact_bundle"),
                patch("trainer.training.pipeline_run_core._oom_check_after_chunk1", return_value=0.5),
                patch("trainer.training.pipeline_run_core.pd.read_parquet", return_value=_seed_df),
            )
            with ExitStack() as stack:
                for p in patches:
                    stack.enter_context(p)
                args = argparse.Namespace(
                    start="2025-01-01",
                    end="2025-06-01",
                    days=None,
                    use_local_parquet=True,
                    force_recompute=False,
                    skip_optuna=True,
                    l2_training_bundle=None,
                )
                run_pipeline(args)
        finally:
            try:
                os.unlink(_tmp_parquet)
            except OSError:
                pass

        mock_links.assert_called_once()
        _call_args = mock_links.call_args[0]
        _train_end = pd.Timestamp(_call_args[1])
        self.assertGreaterEqual(_train_end, expected_effective_start)
        self.assertLessEqual(_train_end, expected_effective_end)

        mock_ensure.assert_called_once_with(
            expected_effective_start,
            expected_effective_end,
            use_local_parquet=True,
            canonical_id_whitelist=None,
            snapshot_interval_days=1,
            preload_sessions=True,
            canonical_map=ANY,
            max_lookback_days=365,
        )

        mock_load_profile.assert_called_once()
        call_args = mock_load_profile.call_args[0]
        self.assertEqual(call_args[0], expected_effective_start)
        self.assertEqual(call_args[1], expected_effective_end)

        self.assertEqual(mock_proc.call_count, 1)
