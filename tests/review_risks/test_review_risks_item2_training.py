"""步驟 4（項目 2）2.2 training 子包搬移 — Code Review 風險點轉成最小可重現測試（tests only，不修改 production）。

對應 STATUS.md « Code Review：步驟 4（項目 2）2.2 training 子包搬移 » §1、§2、§3。
§1：實作位於 trainer/training/trainer.py（BASE_DIR 契約）；頂層 trainer/trainer.py 為 stub，避免 one_time patch 誤改。
§2：from trainer.trainer / trainer.backtester 取得之符號來自實作模組（sys.modules 契約）。
§3：bare import time_fold（trainer/ 在 path）時具 get_monthly_chunks、get_train_valid_test_split 且為 callable。

執行方式（repo 根目錄）：
  python -m pytest tests/test_review_risks_item2_training.py -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
TRAINING_TRAINER = REPO_ROOT / "trainer" / "training" / "trainer.py"
TOPLEVEL_TRAINER = REPO_ROOT / "trainer" / "trainer.py"


# ---------------------------------------------------------------------------
# §1 — 實作位置契約：training/trainer.py 含 BASE_DIR；頂層為 stub
# ---------------------------------------------------------------------------


class TestTrainingImplementationLocationContract(unittest.TestCase):
    """Review §1: Implementation lives in trainer/training/trainer.py; top-level trainer/trainer.py is stub (one_time scripts target wrong file)."""

    def test_implementation_file_contains_base_dir_resolve(self):
        """trainer/training/trainer.py must contain BASE_DIR = Path(__file__).resolve().parent.parent (or equivalent) to lock implementation location."""
        if not TRAINING_TRAINER.exists():
            self.skipTest("trainer/training/trainer.py not found")
        src = TRAINING_TRAINER.read_text(encoding="utf-8")
        self.assertIn(
            "parent.parent",
            src,
            "Implementation must resolve BASE_DIR to trainer/ (STATUS Code Review 2.2 training §1).",
        )
        self.assertTrue(
            "BASE_DIR" in src and ("__file__" in src or "resolve()" in src),
            "trainer/training/trainer.py should define BASE_DIR from __file__ (STATUS Code Review 2.2 training §1).",
        )

    def test_toplevel_trainer_py_remains_stub(self):
        """trainer/trainer.py must remain a thin stub (sys.modules overwrite); one_time scripts that open trainer/trainer.py would otherwise patch wrong file."""
        if not TOPLEVEL_TRAINER.exists():
            self.skipTest("trainer/trainer.py not found")
        src = TOPLEVEL_TRAINER.read_text(encoding="utf-8")
        self.assertIn(
            'sys.modules["trainer.trainer"]',
            src,
            "Top-level trainer/trainer.py must be stub with sys.modules overwrite (STATUS Code Review 2.2 training §1).",
        )
        lines = [ln.strip() for ln in src.splitlines() if ln.strip() and not ln.strip().startswith("#")]
        self.assertLess(
            len(lines),
            20,
            "trainer/trainer.py should be a short stub; if long, implementation may have been merged back (STATUS Code Review 2.2 training §1).",
        )


# ---------------------------------------------------------------------------
# §2 — sys.modules 契約：trainer.trainer / trainer.backtester 解析為實作
# ---------------------------------------------------------------------------


class TestTrainerBacktesterModuleIdentityContract(unittest.TestCase):
    """Review §2: Imports from trainer.trainer and trainer.backtester resolve to implementation module (for mypy/runtime parity)."""

    def test_trainer_trainer_import_resolves_to_implementation(self):
        """from trainer.trainer import run_pipeline, MODEL_DIR must work and run_pipeline must be from trainer.training.trainer."""
        from trainer.trainer import MODEL_DIR, run_pipeline  # noqa: F401

        self.assertEqual(
            run_pipeline.__module__,
            "trainer.training.trainer",
            "run_pipeline should come from implementation module (STATUS Code Review 2.2 training §2).",
        )
        self.assertTrue(callable(run_pipeline))

    def test_trainer_backtester_import_resolves_to_implementation(self):
        """from trainer.backtester import load_dual_artifacts must work and come from trainer.training.backtester."""
        from trainer.backtester import load_dual_artifacts  # noqa: F401

        self.assertEqual(
            load_dual_artifacts.__module__,
            "trainer.training.backtester",
            "load_dual_artifacts should come from implementation module (STATUS Code Review 2.2 training §2).",
        )
        self.assertTrue(callable(load_dual_artifacts))


# ---------------------------------------------------------------------------
# §3 — time_fold bare import 契約：兩函數存在且 callable
# ---------------------------------------------------------------------------


class TestTimeFoldBareImportContract(unittest.TestCase):
    """Review §3: When trainer/ is on sys.path, bare 'import time_fold' must expose get_monthly_chunks and get_train_valid_test_split (callable)."""

    def test_bare_time_fold_has_get_monthly_chunks_and_get_train_valid_test_split(self):
        """With trainer/ on path, import time_fold must provide get_monthly_chunks and get_train_valid_test_split, both callable."""
        trainer_dir = str(REPO_ROOT / "trainer")
        if trainer_dir in sys.path:
            sys.path.remove(trainer_dir)
        # Force load from trainer/time_fold.py (stub) when "time_fold" is requested
        sys.path.insert(0, trainer_dir)
        try:
            if "time_fold" in sys.modules:
                del sys.modules["time_fold"]
            import time_fold as mod  # noqa: F401
        finally:
            sys.path.remove(trainer_dir)
        self.assertTrue(
            hasattr(mod, "get_monthly_chunks"),
            "Bare time_fold must expose get_monthly_chunks (STATUS Code Review 2.2 training §3).",
        )
        self.assertTrue(
            hasattr(mod, "get_train_valid_test_split"),
            "Bare time_fold must expose get_train_valid_test_split (STATUS Code Review 2.2 training §3).",
        )
        self.assertTrue(callable(mod.get_monthly_chunks))
        self.assertTrue(callable(mod.get_train_valid_test_split))
