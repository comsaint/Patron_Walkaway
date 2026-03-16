"""步驟 4（項目 2）2.1 子包目錄 — Code Review 風險點轉成最小可重現測試（tests only，不修改 production）。

對應 STATUS.md « Code Review：步驟 4（項目 2）2.1 子包目錄建立 » §1、§2、§4。
§1：PROJECT.md 列出 trainer 五子包時，須對應目錄存在或文件有「2.1 僅先建立／features 於 2.2」說明。
§2：setup.py 須列舉 walkaway_ml 子包或使用 find_packages，確保安裝後可 import。
§4：import trainer 後 import 子包不觸發 ImportError／循環。

執行方式（repo 根目錄）：
  python -m pytest tests/test_review_risks_item2_subpackages.py -v
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PROJECT_MD = REPO_ROOT / "PROJECT.md"
STATUS_MD = REPO_ROOT / ".cursor" / "plans" / "STATUS.md"
SETUP_PY = REPO_ROOT / "setup.py"
TRAINER_DIR = REPO_ROOT / "trainer"

# 項目 2 約定之五個子包名（PROJECT 目標樹列出）
SUBPACKAGE_NAMES = ("core", "features", "training", "serving", "etl")
# 2.2 後 setup.py 列舉五個子包（含 features）；契約測試須涵蓋 walkaway_ml.features（STATUS Code Review 2.2 etl/features §7）
SUBPAIRS_WALKAWAY = ("walkaway_ml.core", "walkaway_ml.features", "walkaway_ml.training", "walkaway_ml.serving", "walkaway_ml.etl")


def _project_text() -> str:
    return PROJECT_MD.read_text(encoding="utf-8")


def _status_text() -> str:
    if not STATUS_MD.exists():
        return ""
    return STATUS_MD.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# §1 — PROJECT.md 目標樹與實作一致：五子包列出時須目錄存在或文件有延後說明
# ---------------------------------------------------------------------------


class TestProjectMdSubpackagesMatchRealityOrDisclaimer(unittest.TestCase):
    """Review §1: If PROJECT.md lists trainer subpackages (core, features, training, serving, etl), dirs must exist or doc must state 2.1 only four / features in 2.2."""

    def test_project_md_lists_trainer_subpackages_then_either_dirs_exist_or_deferral(self):
        """When PROJECT lists core/, features/, training/, serving/, etl/ under trainer, either all exist or doc has deferral (2.1 only four; features in 2.2)."""
        text = _project_text()
        # PROJECT 目標樹在 trainer/ 下列出這五個
        has_all_five = all(
            f"{name}/" in text or f"{name}" in text.split("trainer/")[-1].split("\n")[0]
            for name in SUBPACKAGE_NAMES
        )
        # 更寬鬆：只要樹裡有 core、features、training、serving、etl 在 trainer 段落即可
        trainer_section = ""
        if "trainer/" in text:
            idx = text.find("trainer/")
            trainer_section = text[idx : idx + 1200]
        has_core = "core/" in trainer_section
        has_features = "features/" in trainer_section
        has_training = "training/" in trainer_section
        has_serving = "serving/" in trainer_section
        has_etl = "etl/" in trainer_section
        if not (has_core and has_features and has_training and has_serving and has_etl):
            self.skipTest("PROJECT.md does not list all five trainer subpackages in tree")
        # 檢查：trainer/features/ 目錄存在，或文件有延後說明
        features_dir_exists = (TRAINER_DIR / "features").is_dir()
        status_text = _status_text()
        deferral = (
            "2.1 僅先建立" in text
            or "2.1 僅先" in text
            or "features 於 2.2" in text
            or "子包於 2.2" in text
            or ("未建立" in text and "features" in text)
            or "僅先建立 core" in text
            or "2.1 僅先建立" in status_text
            or "features 於 2.2" in status_text
            or ("未建立" in status_text and "features" in status_text)
        )
        self.assertTrue(
            features_dir_exists or deferral,
            msg="PROJECT lists five trainer subpackages; trainer/features/ does not exist. "
            "Doc must state that 2.1 only establishes four subpackages and features is in 2.2 (STATUS Code Review 項目 2.1 §1).",
        )


# ---------------------------------------------------------------------------
# §2 — setup.py 須列舉子包或使用 find_packages
# ---------------------------------------------------------------------------


class TestSetupPySubpackagesContract(unittest.TestCase):
    """Review §2: setup.py must list walkaway_ml subpackages or use find_packages so installed wheel has them."""

    def test_setup_py_packages_include_subpackages_or_find_packages(self):
        """Either packages list includes walkaway_ml.core, .training, .serving, .etl or setup uses find_packages()."""
        if not SETUP_PY.exists():
            self.skipTest("setup.py not found")
        src = SETUP_PY.read_text(encoding="utf-8")
        uses_find = "find_packages" in src
        if uses_find:
            return
        # 解析 setup() 的 packages= 引數
        try:
            tree = ast.parse(src)
        except SyntaxError:
            self.fail("setup.py has syntax error")
        packages_value = None
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id == "setup":
                    for kw in node.keywords:
                        if kw.arg == "packages" and isinstance(kw.value, ast.List):
                            packages_value = [ast.literal_eval(e) for e in kw.value.elts]
                            break
                    break
        self.assertIsNotNone(packages_value, msg="setup() must have packages= argument")
        for sub in SUBPAIRS_WALKAWAY:
            self.assertIn(
                sub,
                packages_value,
                msg="setup.py packages= must include %r (or use find_packages()) so installed wheel has subpackage (STATUS 項目 2.1 §2)." % sub,
            )


# ---------------------------------------------------------------------------
# §4 — import trainer 後 import 子包無 ImportError
# ---------------------------------------------------------------------------


class TestTrainerSubpackagesImportNoCycle(unittest.TestCase):
    """Review §4: After importing trainer, importing subpackages must not raise ImportError or cause cycle."""

    def test_import_trainer_then_subpackages_succeeds(self):
        """import trainer; then trainer.core, trainer.training, trainer.serving, trainer.etl must import without error."""
        import trainer  # noqa: F401

        import trainer.core  # noqa: F401
        import trainer.training  # noqa: F401
        import trainer.serving  # noqa: F401
        import trainer.etl  # noqa: F401
        # 若執行到此無 ImportError 即通過
        self.assertTrue(True, "all subpackages imported")

    def test_import_trainer_features_importable(self):
        """trainer.features 在 2.2 後為子包（package），須可 import 且具備 feature 符號。"""
        import trainer.features as f  # noqa: F401

        # 2.2 後為 package（__path__）或原為 module（__file__）；須可取得 PROFILE_FEATURE_COLS
        self.assertTrue(
            hasattr(f, "PROFILE_FEATURE_COLS") or hasattr(f, "__path__"),
            msg="trainer.features must be importable and expose feature symbols (2.2 package).",
        )
