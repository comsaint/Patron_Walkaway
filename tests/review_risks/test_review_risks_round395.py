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
from unittest.mock import patch

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_FEATURES_PY = _REPO_ROOT / "trainer" / "features" / "features.py"  # 項目 2.2: 實作在 features 子包
_SCORER_PY = _REPO_ROOT / "trainer" / "serving" / "scorer.py"


def _scorer_mod():
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))
    return importlib.import_module("trainer.scorer")


# ── Risk #1: features.py FileNotFoundError 警告應使用 features_candidates.yaml / repo spec 用語 ──


class TestR395FeaturesWarningMessage(unittest.TestCase):
    """R395 Review #1: features.py 的 FileNotFoundError 警告應含 features_candidates.yaml 或 repo spec."""

    def test_features_yaml_missing_warning_uses_new_naming(self):
        """當 YAML 不存在時，warning 訊息應使用 features_candidates.yaml 或 repo spec，不再用 template YAML."""
        src = _FEATURES_PY.read_text(encoding="utf-8")
        # 定位 except FileNotFoundError 區塊中的 logger.warning 字串
        idx_except = src.find("except FileNotFoundError:")
        self.assertNotEqual(idx_except, -1, "features.py should have except FileNotFoundError block")
        block = src[idx_except : idx_except + 800]
        # 該區塊內應包含新用語之一（修補後會通過）
        has_new = "features_candidates.yaml" in block or "repo spec" in block
        self.assertTrue(
            has_new,
            "In except FileNotFoundError block, warning message should contain "
            "'features_candidates.yaml' or 'repo spec' (R395 Review #1).",
        )
        # 修補後不應再僅依賴「template YAML」作為唯一說明
        self.assertNotIn(
            "Ensure the template YAML exists",
            block,
            "Warning should not rely on 'template YAML' alone (R395 Review #1).",
        )


# ── Risk #2: Scorer 無 frozen 且無 fallback 時 feature_spec 為 None ──


class TestR395ScorerNoSpecReturnsNone(unittest.TestCase):
    """R395 Review #2: load_dual_artifacts 在無 frozen 且 fallback 路徑不存在時，feature_spec 應為 None."""

    def test_load_dual_artifacts_without_spec_returns_none(self):
        """Temp 目錄無 feature_spec.yaml，且 FEATURE_SPEC_PATH.exists() 為 False 時，artifacts['feature_spec'] 為 None."""
        scorer = _scorer_mod()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            # 建立最小 model.pkl 讓 load_dual_artifacts 能執行到回傳（不因「無 model」而 raise）
            minimal_bundle = {"model": None, "threshold": 0.5, "features": []}
            joblib.dump(minimal_bundle, tmp_path / "model.pkl")
            # 不建立 feature_spec.yaml；fallback 路徑改為不存在
            fake_path = pathlib.Path("/nonexistent/features_candidates.yaml")
            with patch.object(scorer, "FEATURE_SPEC_PATH", fake_path):
                artifacts = scorer.load_dual_artifacts(tmp_path)
            self.assertIsNone(
                artifacts.get("feature_spec"),
                "When no frozen spec and FEATURE_SPEC_PATH does not exist, "
                "load_dual_artifacts should return feature_spec=None (R395 Review #2).",
            )


# ── Risk #3: Scorer 註解不應再寫「global template」 ──


class TestR395ScorerFallbackComment(unittest.TestCase):
    """R395 Review #3: scorer 的 fallback 註解應與檔名重構一致，不含「global template」."""

    def test_scorer_fallback_comment_does_not_say_global_template(self):
        """Fall back 註解應改為 repo feature spec / features_candidates.yaml，不含 global template."""
        src = _SCORER_PY.read_text(encoding="utf-8")
        idx = src.find("Fall back to the")
        self.assertNotEqual(idx, -1, "scorer.py should have fallback comment")
        comment_block = src[idx : idx + 200]
        self.assertNotIn(
            "global template",
            comment_block,
            "Fallback comment should not say 'global template' (R395 Review #3); "
            "use 'repo feature spec' or 'features_candidates.yaml'.",
        )


# ── Risk #4: doc/one_time_scripts 路徑以 __file__ 為基準會指向錯誤目錄 ──


class TestR395ScriptsOneTimeSpecPath(unittest.TestCase):
    """R395 Review #4: doc/one_time_scripts 的 Path(__file__).parent 會解析到錯誤路徑，僅文件化現狀."""

    def test_script_resolved_spec_path_does_not_exist(self):
        """以 doc/one_time_scripts 為基準解析的 feature_spec 路徑不存在（pre-existing 問題）."""
        script_spec = _REPO_ROOT / "doc" / "one_time_scripts" / "feature_spec" / "features_candidates.yaml"
        self.assertFalse(
            script_spec.exists(),
            "Path as resolved from doc/one_time_scripts (__file__.parent) does not exist; "
            "spec lives under trainer/feature_spec/ (R395 Review #4).",
        )

    def test_trainer_spec_path_exists(self):
        """Repo 內唯一 spec 位於 trainer/feature_spec/features_candidates.yaml."""
        trainer_spec = _REPO_ROOT / "trainer" / "feature_spec" / "features_candidates.yaml"
        self.assertTrue(
            trainer_spec.exists(),
            "Canonical feature spec must exist at trainer/feature_spec/features_candidates.yaml.",
        )
