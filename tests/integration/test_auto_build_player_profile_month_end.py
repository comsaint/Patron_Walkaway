"""Tests for auto_build_player_profile --month-end (Code review §4)."""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import trainer.scripts.auto_build_player_profile as auto_mod


class TestAutoBuildMonthEndDoesNotSaveCheckpoint(unittest.TestCase):
    """Code review §4: month-end run does not call save_checkpoint (single shot, no resume)."""

    def test_month_end_success_does_not_save_checkpoint(self):
        with patch.object(
            auto_mod,
            "run_etl_chunk",
            return_value=auto_mod.CmdResult(returncode=0, stdout="", stderr=""),
        ), patch.object(auto_mod, "save_checkpoint") as mock_save:
            auto_mod.auto_run(
                start_date=date(2026, 1, 1),
                end_date=date(2026, 1, 31),
                checkpoint_file=Path(tempfile.gettempdir()) / "test_ckpt_month_end.json",
                local_parquet=True,
                chunk_days=1,
                resume=False,
                month_end=True,
            )
            mock_save.assert_not_called()
