"""步驟 4（項目 2）2.2 core 子包搬移 — Code Review 風險點轉成最小可重現測試（tests only，不修改 production）。

對應 STATUS.md « Code Review：步驟 4（項目 2）2.2 core 子包搬移 » §1、§2、§3。
§1：config.DEFAULT_MODEL_DIR／DEFAULT_BACKTEST_OUT 存在且為 Path（deploy 時須設 MODEL_DIR 之契約）。
§2：頂層 re-export 須暴露 _REPO_ROOT，且與 trainer.core.config._REPO_ROOT 同一物件；DEFAULT_MODEL_DIR 與 core 一致。
§3：依序 import trainer、trainer.core、trainer.config、trainer.db_conn 無 ImportError（無循環）。

執行方式（repo 根目錄）：
  python -m pytest tests/test_review_risks_item2_core_move.py -v
"""

from __future__ import annotations

import unittest
from pathlib import Path


# ---------------------------------------------------------------------------
# §1 — config 預設路徑存在且為 Path
# ---------------------------------------------------------------------------


class TestConfigDefaultPathsExistAndArePath(unittest.TestCase):
    """Review §1: DEFAULT_MODEL_DIR and DEFAULT_BACKTEST_OUT must exist and be Path (contract for deploy: set MODEL_DIR)."""

    def test_trainer_config_default_model_dir_exists_and_is_path(self):
        """trainer.config.DEFAULT_MODEL_DIR must exist and be a Path."""
        import trainer.config as config

        self.assertTrue(
            hasattr(config, "DEFAULT_MODEL_DIR"),
            "trainer.config must expose DEFAULT_MODEL_DIR (re-export from core)",
        )
        self.assertIsInstance(
            config.DEFAULT_MODEL_DIR,
            Path,
            "DEFAULT_MODEL_DIR must be a Path (STATUS Code Review 2.2 core §1).",
        )

    def test_trainer_config_default_backtest_out_exists_and_is_path(self):
        """trainer.config.DEFAULT_BACKTEST_OUT must exist and be a Path."""
        import trainer.config as config

        self.assertTrue(
            hasattr(config, "DEFAULT_BACKTEST_OUT"),
            "trainer.config must expose DEFAULT_BACKTEST_OUT (re-export from core)",
        )
        self.assertIsInstance(
            config.DEFAULT_BACKTEST_OUT,
            Path,
            "DEFAULT_BACKTEST_OUT must be a Path (STATUS Code Review 2.2 core §1).",
        )


# ---------------------------------------------------------------------------
# §2 — 頂層 re-export 須暴露 _REPO_ROOT 且與 core 同一物件
# ---------------------------------------------------------------------------


class TestConfigReexportUnderscoreRepoRoot(unittest.TestCase):
    """Review §2: trainer.config must expose _REPO_ROOT and it must be the same object as trainer.core.config._REPO_ROOT."""

    def test_trainer_config_has_repo_root_and_same_as_core(self):
        """trainer.config._REPO_ROOT must exist and be the same object as trainer.core.config._REPO_ROOT."""
        import trainer.config as config
        import trainer.core.config as core_config

        self.assertTrue(
            hasattr(config, "_REPO_ROOT"),
            "trainer.config must explicitly re-export _REPO_ROOT (import * does not export underscore names; STATUS 2.2 §2).",
        )
        self.assertIs(
            config._REPO_ROOT,
            core_config._REPO_ROOT,
            "trainer.config._REPO_ROOT must be the same object as trainer.core.config._REPO_ROOT.",
        )

    def test_trainer_config_default_model_dir_same_as_core(self):
        """trainer.config.DEFAULT_MODEL_DIR must be the same object as trainer.core.config.DEFAULT_MODEL_DIR."""
        import trainer.config as config
        import trainer.core.config as core_config

        self.assertIs(
            config.DEFAULT_MODEL_DIR,
            core_config.DEFAULT_MODEL_DIR,
            "Re-export must expose same DEFAULT_MODEL_DIR object as core.",
        )


# ---------------------------------------------------------------------------
# §3 — 依序 import 無循環／ImportError
# ---------------------------------------------------------------------------


class TestCoreImportNoCycle(unittest.TestCase):
    """Review §3: Importing trainer, trainer.core, trainer.config, trainer.db_conn in sequence must not raise."""

    def test_import_trainer_core_config_db_conn_schema_io_duckdb_schema_no_error(self):
        """Import trainer, trainer.core, trainer.config, trainer.db_conn, trainer.schema_io, trainer.duckdb_schema without ImportError."""
        import trainer  # noqa: F401
        import trainer.core  # noqa: F401
        import trainer.config  # noqa: F401
        import trainer.db_conn  # noqa: F401
        import trainer.schema_io  # noqa: F401
        import trainer.duckdb_schema  # noqa: F401
        self.assertTrue(True, "all imports succeeded")
