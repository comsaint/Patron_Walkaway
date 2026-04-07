"""Integration smoke: run_train_backtest_investigation_windows CLI (dry-run only)."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from trainer.scripts import run_train_backtest_investigation_windows as e2e_mod


class TestInvestigationE2eScript(unittest.TestCase):
    """Do not run real train/backtest in CI; dry-run and argv builders only."""

    def test_dry_run_returns_zero(self) -> None:
        """run_pipeline(..., dry_run=True) must succeed without subprocess."""
        rc = e2e_mod.run_pipeline(
            train_start=e2e_mod._DEFAULT_TRAIN_START,
            train_end=e2e_mod._DEFAULT_TRAIN_END,
            backtest_start=e2e_mod._DEFAULT_BACKTEST_START,
            backtest_end=e2e_mod._DEFAULT_BACKTEST_END,
            use_local_parquet=False,
            skip_train_optuna=False,
            skip_backtest_optuna=False,
            recent_chunks=None,
            sample_rated=None,
            no_preload=False,
            model_version=None,
            model_dir=None,
            train_only=False,
            backtest_only=False,
            dry_run=True,
        )
        self.assertEqual(rc, 0)

    def test_build_train_cmd_forwards_flags(self) -> None:
        """Train argv must include optional trainer flags when set."""
        cmd = e2e_mod._build_train_cmd(
            train_start="2024-01-01",
            train_end="2025-12-31",
            use_local_parquet=True,
            skip_optuna=True,
            recent_chunks=3,
            sample_rated=100,
            no_preload=True,
        )
        self.assertEqual(cmd[0:5], [e2e_mod.sys.executable, "-m", "trainer.trainer", "--start", "2024-01-01"])
        self.assertIn("--use-local-parquet", cmd)
        self.assertIn("--skip-optuna", cmd)
        self.assertIn("--recent-chunks", cmd)
        self.assertIn("3", cmd)
        self.assertIn("--sample-rated", cmd)
        self.assertIn("100", cmd)
        self.assertIn("--no-preload", cmd)

    def test_build_backtest_cmd_model_dir_overrides_version(self) -> None:
        """--model-dir should appear when path is given."""
        p = Path("out/models/some-version")
        cmd = e2e_mod._build_backtest_cmd(
            backtest_start="2026-01-01",
            backtest_end="2026-03-31",
            use_local_parquet=False,
            skip_optuna=False,
            model_version="ignored-when-dir-set",
            model_dir=p,
        )
        self.assertIn("--model-dir", cmd)
        idx = cmd.index("--model-dir")
        self.assertEqual(cmd[idx + 1], str(p))
        self.assertNotIn("--model-version", cmd)

    def test_main_rejects_train_and_backtest_only(self) -> None:
        """Conflicting modes must exit 2."""
        stderr = io.StringIO()
        with patch.object(e2e_mod.sys, "argv", ["prog", "--train-only", "--backtest-only"]):
            with redirect_stderr(stderr):
                rc = e2e_mod.main()
        self.assertEqual(rc, 2)
        self.assertIn("Cannot combine", stderr.getvalue())

    def test_repo_root_points_at_workspace(self) -> None:
        """Script lives under trainer/scripts → repo root is two parents up."""
        root = e2e_mod._repo_root()
        self.assertTrue((root / "trainer" / "scripts" / "run_train_backtest_investigation_windows.py").is_file())
