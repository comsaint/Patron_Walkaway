"""Round 395 Review — Feature Spec 檔名重構 風險點 → 最小可重現測試（tests-only）.

對應 STATUS.md Round 395 Review 所列風險；本檔僅新增測試，不修改 production code。
通過條件：當 production 依 Review 建議修補後，對應測試應由紅轉綠。
"""

from __future__ import annotations

import importlib
import joblib
import pathlib
import sys
import tempfile
import unittest
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_FEATURES_PY = _REPO_ROOT / "trainer" / "features" / "features.py"  # 項目 2.2: 實作在 features 子包
_SCORER_PY = _REPO_ROOT / "trainer" / "serving" / "scorer.py"


def _scorer_mod():
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))
    return importlib.import_module("trainer.serving.scorer")


# ── Risk #1: features.py FileNotFoundError 警告應使用 feature_candidates / repo 路徑用語 ──


class TestR395FeaturesWarningMessage(unittest.TestCase):
    """R395 Review #1: features.py 的 FileNotFoundError 警告應含 feature_candidates 或 trainer/feature_spec 路徑。"""

    def test_features_yaml_missing_warning_uses_new_naming(self):
        """當 YAML 不存在時，warning 訊息應指向 trainer/feature_spec/feature_candidates.yaml。"""
        src = _FEATURES_PY.read_text(encoding="utf-8")
        # 定位 except FileNotFoundError 區塊中的 logger.warning 字串
        idx_except = src.find("except FileNotFoundError:")
        self.assertNotEqual(idx_except, -1, "features.py should have except FileNotFoundError block")
        block = src[idx_except : idx_except + 800]
        # 該區塊內應包含新用語之一（修補後會通過）
        has_new = "feature_candidates.yaml" in block or "trainer/feature_spec" in block
        self.assertTrue(
            has_new,
            "In except FileNotFoundError block, warning message should contain "
            "'feature_candidates.yaml' or 'trainer/feature_spec' (R395 Review #1).",
        )
        # 修補後不應再僅依賴「template YAML」作為唯一說明
        self.assertNotIn(
            "Ensure the template YAML exists",
            block,
            "Warning should not rely on 'template YAML' alone (R395 Review #1).",
        )


# ── Risk #2: Scorer bundle-only：無 bundle 內 feature_spec 時應 fail fast ──


class TestR395ScorerBundleOnlyRaises(unittest.TestCase):
    """R395（更新）: load_dual_artifacts 必須讀 bundle 內凍結 feature_spec.yaml，無檔則拋錯。"""

    def test_load_dual_artifacts_without_bundle_spec_raises(self):
        """Temp 目錄無 feature_spec.yaml 時應 FileNotFoundError（不再 fallback repo）。"""
        scorer = _scorer_mod()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            minimal_bundle = {"model": None, "threshold": 0.5, "features": []}
            joblib.dump(minimal_bundle, tmp_path / "model.pkl")
            with self.assertRaises(FileNotFoundError) as ctx:
                scorer.load_dual_artifacts(tmp_path)
            self.assertIn("Bundle-only", str(ctx.exception))


# ── Risk #3: Scorer 註解應描述 bundle-only 契約 ──


class TestR395ScorerBundleOnlyComment(unittest.TestCase):
    """R395 Review #3（更新）: scorer load_dual_artifacts 應以 bundle-only 註解描述凍結 spec。"""

    def test_scorer_load_dual_mentions_bundle_only_contract(self):
        """load_dual_artifacts 區塊應含 Bundle-only runtime contract。"""
        src = _SCORER_PY.read_text(encoding="utf-8")
        idx = src.find("def load_dual_artifacts")
        self.assertNotEqual(idx, -1, "scorer.py should define load_dual_artifacts")
        # Contract comment lives after docstring / early MODEL_DIR resolution.
        block = src[idx : idx + 4500]
        self.assertIn(
            "Bundle-only",
            block,
            "load_dual_artifacts should document bundle-only frozen spec contract.",
        )


# ── Risk #4: doc/one_time_scripts 路徑以 __file__ 為基準會指向錯誤目錄 ──


class TestR395ScriptsOneTimeSpecPath(unittest.TestCase):
    """R395 Review #4: doc/one_time_scripts 的 Path(__file__).parent 會解析到錯誤路徑，僅文件化現狀."""

    def test_script_resolved_spec_path_does_not_exist(self):
        """以 doc/one_time_scripts 為基準解析的 feature_spec 路徑不存在（pre-existing 問題）."""
        script_spec = _REPO_ROOT / "doc" / "one_time_scripts" / "feature_spec" / "feature_spec.yaml"
        self.assertFalse(
            script_spec.exists(),
            "Path as resolved from doc/one_time_scripts (__file__.parent) does not exist; "
            "spec lives under trainer/feature_spec/ (R395 Review #4).",
        )

    def test_trainer_candidates_path_exists(self):
        """Repo 內 dev 候選 catalog 位於 trainer/feature_spec/feature_candidates.yaml."""
        trainer_spec = _REPO_ROOT / "trainer" / "feature_spec" / "feature_candidates.yaml"
        self.assertTrue(
            trainer_spec.exists(),
            "Canonical feature candidates must exist at trainer/feature_spec/feature_candidates.yaml.",
        )
