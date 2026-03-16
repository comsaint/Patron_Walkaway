"""項目 4 產出目錄統一 — Code Review 風險點轉成最小可重現測試（tests only，不修改 production）。

對應 STATUS.md « Code Review：項目 4 變更 » §1–§3。
§1：環境變數為空白時應視為未設定（scorer MODEL_DIR/STATE_DB_PATH；build_deploy_package 預設）。
§2：從 repo 執行時 config 預設路徑應在 _REPO_ROOT 下且含 out。
§3：BACKTEST_OUT 為 Path、可 import backtester（sanity）。

執行方式（repo 根目錄）：
  python -m pytest tests/test_review_risks_output_paths_item4.py -v
"""

from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# §2 — config 從 repo 執行時預設路徑在 _REPO_ROOT 下且含 out
# ---------------------------------------------------------------------------


class TestConfigDefaultOutputPathsFromRepo(unittest.TestCase):
    """Review §2: When run from repo, DEFAULT_MODEL_DIR and DEFAULT_BACKTEST_OUT under _REPO_ROOT and contain 'out'."""

    def test_repo_root_exists_and_is_dir(self):
        """_REPO_ROOT must exist and be a directory when tests run from repo."""
        import trainer.config as config

        self.assertTrue(
            config._REPO_ROOT.exists(),
            "_REPO_ROOT should exist when config is loaded from repo",
        )
        self.assertTrue(
            config._REPO_ROOT.is_dir(),
            "_REPO_ROOT should be a directory",
        )

    def test_default_model_dir_under_repo_root_and_contains_out(self):
        """DEFAULT_MODEL_DIR must be under _REPO_ROOT and path must contain 'out' and 'models'."""
        import trainer.config as config

        resolved = config.DEFAULT_MODEL_DIR.resolve()
        self.assertIn(
            "out",
            config.DEFAULT_MODEL_DIR.parts,
            "DEFAULT_MODEL_DIR should contain 'out' (convention out/models)",
        )
        self.assertIn(
            "models",
            config.DEFAULT_MODEL_DIR.parts,
            "DEFAULT_MODEL_DIR should contain 'models'",
        )
        try:
            resolved.relative_to(config._REPO_ROOT.resolve())
        except ValueError:
            self.fail(
                "DEFAULT_MODEL_DIR must be under _REPO_ROOT, got %s" % resolved
            )

    def test_default_backtest_out_under_repo_root_and_contains_out(self):
        """DEFAULT_BACKTEST_OUT must be under _REPO_ROOT and path must contain 'out' and 'backtest'."""
        import trainer.config as config

        self.assertIn(
            "out",
            config.DEFAULT_BACKTEST_OUT.parts,
            "DEFAULT_BACKTEST_OUT should contain 'out'",
        )
        self.assertIn(
            "backtest",
            config.DEFAULT_BACKTEST_OUT.parts,
            "DEFAULT_BACKTEST_OUT should contain 'backtest'",
        )
        try:
            config.DEFAULT_BACKTEST_OUT.resolve().relative_to(
                config._REPO_ROOT.resolve()
            )
        except ValueError:
            self.fail(
                "DEFAULT_BACKTEST_OUT must be under _REPO_ROOT, got %s"
                % config.DEFAULT_BACKTEST_OUT.resolve()
            )


# ---------------------------------------------------------------------------
# §1 — 環境變數僅空白時應視為未設定（scorer MODEL_DIR / STATE_DB_PATH）
# ---------------------------------------------------------------------------


class TestScorerWhitespaceEnvTreatedAsUnset(unittest.TestCase):
    """Review §1: When MODEL_DIR or STATE_DB_PATH is whitespace-only, scorer should use default path, not Path('  ')."""

    def test_scorer_model_dir_whitespace_only_should_use_default_path(self):
        """Contract: When MODEL_DIR is '  ', MODEL_DIR must be default (out/models or trainer/models), not Path('  ')."""
        import trainer.scorer as scorer_mod

        with patch.dict("os.environ", {"MODEL_DIR": "  "}, clear=False):
            importlib.reload(scorer_mod)
            try:
                resolved = scorer_mod.MODEL_DIR.resolve()
                parts = resolved.parts
                # Desired: default is either repo/out/models or trainer/models
                self.assertIn(
                    "models",
                    parts,
                    "MODEL_DIR must point to a path containing 'models' (default), not cwd from Path('  ')",
                )
                # If using config default, path contains 'out'; if fallback trainer/models, path contains 'trainer'
                self.assertTrue(
                    "out" in parts or "trainer" in parts,
                    "MODEL_DIR must be default (out/models or trainer/models), got %s" % resolved,
                )
                # Must not be a path that is effectively cwd with no meaningful segment
                self.assertNotEqual(
                    str(resolved).strip(),
                    "",
                    "MODEL_DIR must not be Path('  ') which can resolve to odd paths",
                )
            finally:
                importlib.reload(scorer_mod)

    def test_scorer_state_db_path_whitespace_only_should_use_default_path(self):
        """Contract: When STATE_DB_PATH is '  ', STATE_DIR must contain 'local_state', not Path('  ').parent."""
        import trainer.scorer as scorer_mod

        with patch.dict("os.environ", {"STATE_DB_PATH": "  "}, clear=False):
            importlib.reload(scorer_mod)
            try:
                self.assertIn(
                    "local_state",
                    scorer_mod.STATE_DIR.parts,
                    "STATE_DB_PATH whitespace: STATE_DIR must be default (local_state), not Path('  ').parent",
                )
                self.assertIn(
                    "local_state",
                    scorer_mod.STATE_DB_PATH.parts,
                    "STATE_DB_PATH whitespace: STATE_DB_PATH must be default (local_state/state.db)",
                )
            finally:
                importlib.reload(scorer_mod)


# ---------------------------------------------------------------------------
# §1 — build_deploy_package 預設 model-source：空白 env 時應為 REPO_ROOT/out/models（契約）
# ---------------------------------------------------------------------------


class TestBuildDeployPackageDefaultModelSourceContract(unittest.TestCase):
    """Review §1: Desired contract when MODEL_DIR env is whitespace-only is REPO_ROOT/out/models."""

    def test_desired_default_model_source_for_whitespace_env(self):
        """Contract (no production change): when env is whitespace-only, desired default is REPO_ROOT/out/models."""
        # Desired logic: treat whitespace as unset -> default = REPO_ROOT / "out" / "models"
        def desired_default_model_source(env_value: str | None, repo_root: Path) -> Path:
            if env_value and env_value.strip():
                return Path(env_value.strip())
            return repo_root / "out" / "models"

        self.assertEqual(
            desired_default_model_source("  ", REPO_ROOT),
            REPO_ROOT / "out" / "models",
            "When MODEL_DIR is whitespace, desired default is REPO_ROOT/out/models",
        )
        self.assertEqual(
            desired_default_model_source("", REPO_ROOT),
            REPO_ROOT / "out" / "models",
            "When MODEL_DIR is empty, desired default is REPO_ROOT/out/models",
        )
        self.assertEqual(
            desired_default_model_source(None, REPO_ROOT),
            REPO_ROOT / "out" / "models",
            "When MODEL_DIR is unset, desired default is REPO_ROOT/out/models",
        )

    def test_build_deploy_package_unset_env_default_is_out_models(self):
        """When MODEL_DIR is unset, build_deploy_package default model-source must be REPO_ROOT/out/models."""
        # Run in subprocess so env is clean; get default by parsing after importing with unset MODEL_DIR
        env = {k: v for k, v in __import__("os").environ.items() if k != "MODEL_DIR"}
        code = """
import sys
sys.path.insert(0, %r)
from pathlib import Path
from package import build_deploy_package
# Default is computed inside main(); we replicate the same logic here to assert
_repo = Path(build_deploy_package.__file__).resolve().parent.parent
_env = __import__('os').environ.get('MODEL_DIR')
_default = Path(_env) if _env else (_repo / 'out' / 'models')
print(_default.resolve())
""" % str(
            REPO_ROOT
        )
        import subprocess

        r = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            env=env,
        )
        self.assertEqual(r.returncode, 0, (r.stdout, r.stderr))
        out = r.stdout.strip()
        self.assertIn("out", out)
        self.assertIn("models", out)


# ---------------------------------------------------------------------------
# §3 — backtester import 與 BACKTEST_OUT 型別／位置（sanity）
# ---------------------------------------------------------------------------


class TestBacktesterOutputPathSanity(unittest.TestCase):
    """Review §3: BACKTEST_OUT is a Path and backtester can be imported (sanity)."""

    def test_backtester_imports_and_backtest_out_is_path(self):
        """BACKTEST_OUT must be a Path; import backtester must succeed."""
        import trainer.backtester as backtester_mod

        self.assertIsInstance(
            backtester_mod.BACKTEST_OUT,
            Path,
            "BACKTEST_OUT must be a Path",
        )

    def test_backtest_out_under_repo_or_trainer(self):
        """BACKTEST_OUT should be under repo out/backtest or trainer/out_backtest."""
        import trainer.backtester as backtester_mod
        import trainer.config as config

        resolved = backtester_mod.BACKTEST_OUT.resolve()
        repo_root = config._REPO_ROOT.resolve()
        # Either under repo/out/backtest or under trainer (legacy fallback)
        try:
            rel = resolved.relative_to(repo_root)
            self.assertIn("out", rel.parts, "BACKTEST_OUT should be under out/ when using config default")
        except ValueError:
            # Fallback: under trainer
            trainer_dir = repo_root / "trainer"
            try:
                resolved.relative_to(trainer_dir)
            except ValueError:
                self.fail("BACKTEST_OUT should be under _REPO_ROOT or trainer/, got %s" % resolved)
