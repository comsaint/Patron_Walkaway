"""Round 389 Code Review — Round 388 動態 RAM 預算風險點轉最小可重現測試。

STATUS.md Round 389 Review: 將 Reviewer 提到的風險點（#1–#6，不含 #7 安全性結論）
轉成 tests-only 的最小可重現測試或 source 契約。不修改 production code。

對應項目：1=available_bytes<=0, 2=config 型別, 3=SET memory_limit 格式, 4=threads 預設,
5=極大 available, 6=psutil 失敗 fallback。
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

from trainer.trainer import (
    _CANONICAL_MAP_SESSION_COLS,
    _compute_canonical_map_duckdb_budget,
    build_canonical_links_and_dummy_from_duckdb,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_TRAINER_PATH = _REPO_ROOT / "trainer" / "trainer.py"
_TRAINER_SRC = _TRAINER_PATH.read_text(encoding="utf-8")


def _minimal_valid_session_parquet(path: Path, train_end: datetime) -> None:
    """Minimal session parquet so schema/config checks pass."""
    one = pd.DataFrame([{
        "session_id": "s1",
        "player_id": 10,
        "casino_player_id": " C1 ",
        "lud_dtm": pd.Timestamp("2025-01-15 12:00:00"),
        "session_start_dtm": pd.Timestamp("2025-01-15 11:00:00"),
        "session_end_dtm": pd.Timestamp("2025-01-15 12:00:00"),
        "is_manual": 0,
        "is_deleted": 0,
        "is_canceled": 0,
        "num_games_with_wager": 1,
        "turnover": 10.0,
    }])
    cols = [c for c in _CANONICAL_MAP_SESSION_COLS if c in one.columns]
    one[cols].to_parquet(path, index=False)


# ---------------------------------------------------------------------------
# Review #1 — available_bytes <= 0 時 budget 為 MIN_GB（bytes）
# ---------------------------------------------------------------------------

class TestR389_1_ZeroAvailableReturnsMinGb(unittest.TestCase):
    """Review #1: When available_bytes is 0, budget should be MIN_GB (bytes)."""

    def test_compute_budget_zero_returns_min_gb_bytes(self):
        """_compute_canonical_map_duckdb_budget(0) returns int(MIN_GB * 1024**3) with default config."""
        import trainer.config as config

        min_gb = getattr(config, "CANONICAL_MAP_DUCKDB_MEMORY_LIMIT_MIN_GB", 1.0)
        expected = int(min_gb * 1024**3)
        result = _compute_canonical_map_duckdb_budget(0)
        self.assertEqual(result, expected, "available_bytes=0 should yield MIN_GB in bytes (Review #1)")


# ---------------------------------------------------------------------------
# Review #2 — config 非數值時應拋出異常（不靜默傳入 DuckDB）
# ---------------------------------------------------------------------------

class TestR389_2_InvalidConfigTypeRaises(unittest.TestCase):
    """Review #2: Non-numeric RAM_FRACTION (or MIN/MAX_GB) should raise, not pass through to DuckDB."""

    def test_ram_fraction_string_raises(self):
        """When CANONICAL_MAP_DUCKDB_RAM_FRACTION is a string, _compute_canonical_map_duckdb_budget raises."""
        fake_cfg = MagicMock()
        fake_cfg.CANONICAL_MAP_DUCKDB_RAM_FRACTION = "0.5"
        fake_cfg.CANONICAL_MAP_DUCKDB_MEMORY_LIMIT_MIN_GB = 1.0
        fake_cfg.CANONICAL_MAP_DUCKDB_MEMORY_LIMIT_MAX_GB = 24.0

        with patch("trainer.trainer._cfg", fake_cfg):
            with self.assertRaises(Exception):
                _compute_canonical_map_duckdb_budget(None)


# ---------------------------------------------------------------------------
# Review #3 — SET memory_limit 被呼叫且含 GB（可選：日後 production 用 .2f 時可加強為兩位小數）
# ---------------------------------------------------------------------------

class TestR389_3_SetMemoryLimitCalledWithGbString(unittest.TestCase):
    """Review #3: build_canonical_links_and_dummy_from_duckdb must call SET memory_limit with a string containing GB."""

    def test_set_memory_limit_called_with_gb(self):
        """With mocked DuckDB, SET memory_limit must be called with a string containing 'GB'."""
        mock_duckdb = MagicMock()
        mock_con = MagicMock()
        call_args = []

        def capture_execute(*args, **kwargs):
            call_args.append(args[0] if args else None)
            result = MagicMock()
            result.df.return_value = pd.DataFrame(
                columns=["player_id", "casino_player_id", "lud_dtm"]
            )
            return result

        mock_con.execute.side_effect = capture_execute
        mock_duckdb.connect.return_value = mock_con

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.parquet"
            _minimal_valid_session_parquet(path, datetime(2025, 2, 1))
            with patch.dict("sys.modules", {"duckdb": mock_duckdb}):
                build_canonical_links_and_dummy_from_duckdb(path, datetime(2025, 2, 1))

        set_mem_calls = [a for a in call_args if a and "memory_limit" in str(a) and "GB" in str(a)]
        self.assertTrue(
            set_mem_calls,
            "SET memory_limit must be called with a string containing GB (Review #3). "
            "Future: when production uses .2f, assert format matches r'\\d+\\.\\d{2}GB'.",
        )


# ---------------------------------------------------------------------------
# Review #4 — 無 patch 時使用的 threads 應等於 config 預設（可選）
# ---------------------------------------------------------------------------

class TestR389_4_ThreadsUsesConfigValue(unittest.TestCase):
    """Review #4: When not patched, SET threads should match config.CANONICAL_MAP_DUCKDB_THREADS."""

    def test_set_threads_matches_config_default(self):
        """With mocked DuckDB, first SET threads call should use config default (e.g. 1)."""
        import trainer.config as config

        mock_duckdb = MagicMock()
        mock_con = MagicMock()
        call_args = []

        def capture_execute(*args, **kwargs):
            call_args.append(args[0] if args else None)
            result = MagicMock()
            result.df.return_value = pd.DataFrame(
                columns=["player_id", "casino_player_id", "lud_dtm"]
            )
            return result

        mock_con.execute.side_effect = capture_execute
        mock_duckdb.connect.return_value = mock_con

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.parquet"
            _minimal_valid_session_parquet(path, datetime(2025, 2, 1))
            with patch.dict("sys.modules", {"duckdb": mock_duckdb}):
                build_canonical_links_and_dummy_from_duckdb(path, datetime(2025, 2, 1))

        expected_threads = getattr(config, "CANONICAL_MAP_DUCKDB_THREADS", 1)
        set_threads_calls = [a for a in call_args if a and "SET threads" in str(a)]
        self.assertTrue(
            set_threads_calls,
            "At least one SET threads call should be recorded.",
        )
        self.assertIn(
            str(expected_threads),
            set_threads_calls[0],
            "SET threads value should match config.CANONICAL_MAP_DUCKDB_THREADS (Review #4).",
        )


# ---------------------------------------------------------------------------
# Review #5 — 極大 available_bytes 時 budget 不超過 MAX_GB（可選）
# ---------------------------------------------------------------------------

class TestR389_5_LargeAvailableClampedToMax(unittest.TestCase):
    """Review #5: Very large available_bytes should be clamped to MAX_GB."""

    def test_compute_budget_large_available_returns_max_gb_bytes(self):
        """_compute_canonical_map_duckdb_budget(2**60) returns int(MAX_GB * 1024**3)."""
        import trainer.config as config

        max_gb = getattr(config, "CANONICAL_MAP_DUCKDB_MEMORY_LIMIT_MAX_GB", 24.0)
        expected = int(max_gb * 1024**3)
        result = _compute_canonical_map_duckdb_budget(2**60)
        self.assertEqual(
            result,
            expected,
            "Very large available_bytes should be clamped to MAX_GB (Review #5).",
        )


# ---------------------------------------------------------------------------
# Review #6 — 無 psutil 或 virtual_memory 失敗時仍完成 mapping 且使用 MIN_GB
# ---------------------------------------------------------------------------

class TestR389_6_PsutilFailureFallbackToMinGb(unittest.TestCase):
    """Review #6: When psutil is missing or virtual_memory() raises, function still completes with MIN_GB."""

    def test_psutil_virtual_memory_raises_still_returns_links_and_dummy(self):
        """Patch psutil.virtual_memory to raise; build_canonical_links_and_dummy_from_duckdb still returns."""
        mock_psutil = MagicMock()
        mock_psutil.virtual_memory.side_effect = Exception("no psutil")

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.parquet"
            _minimal_valid_session_parquet(path, datetime(2025, 2, 1))
            with patch.dict("sys.modules", {"psutil": mock_psutil}):
                links_df, dummy_pids = build_canonical_links_and_dummy_from_duckdb(
                    path, datetime(2025, 2, 1)
                )
        self.assertIsInstance(links_df, pd.DataFrame)
        self.assertIsInstance(dummy_pids, set)
        self.assertIn("player_id", links_df.columns)
        self.assertIn("casino_player_id", links_df.columns)
        self.assertIn("lud_dtm", links_df.columns)


if __name__ == "__main__":
    unittest.main()
