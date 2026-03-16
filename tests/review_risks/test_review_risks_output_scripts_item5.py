"""項目 5 根目錄與零散腳本 — Code Review 風險點轉成最小可重現測試（tests only，不修改 production）。

對應 STATUS.md « Code Review：項目 5 變更 » §1–§2。
§1：check_span.py 須自 repo root 執行（CWD）、空結果時 df[col][0] 會 IndexError（契約／source 文件化）。
§2：doc/one_time_scripts 內 patch 腳本須自 repo root 執行；自錯誤 cwd 執行應失敗且不改動 trainer/*.py。

執行方式（repo 根目錄）：
  python -m pytest tests/test_review_risks_output_scripts_item5.py -v
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CHECK_SPAN_PATH = REPO_ROOT / "scripts" / "check_span.py"
ONE_TIME_DIR = REPO_ROOT / "doc" / "one_time_scripts"
TRAINER_BACKTESTER = REPO_ROOT / "trainer" / "backtester.py"


# ---------------------------------------------------------------------------
# §1 — check_span.py：自非 repo root 的 cwd 執行應失敗
# ---------------------------------------------------------------------------


class TestCheckSpanRequiresRepoRoot(unittest.TestCase):
    """Review §1: check_span.py uses relative path data/...; run from non-root cwd must not succeed."""

    def test_check_span_from_scripts_cwd_fails_with_nonzero_exit(self):
        """When cwd is scripts/, data/gmwds_t_session.parquet is scripts/data/... which does not exist → non-zero exit."""
        if not CHECK_SPAN_PATH.exists():
            self.skipTest("scripts/check_span.py not found")
        proc = subprocess.run(
            [sys.executable, str(CHECK_SPAN_PATH)],
            cwd=REPO_ROOT / "scripts",
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertNotEqual(
            proc.returncode,
            0,
            "check_span.py must fail when run with cwd=scripts/ (data/ not visible); got exit 0",
        )

    def test_check_span_from_one_time_dir_cwd_fails_with_nonzero_exit(self):
        """When cwd is doc/one_time_scripts/, data/ is not repo data/ → non-zero exit."""
        if not CHECK_SPAN_PATH.exists():
            self.skipTest("scripts/check_span.py not found")
        if not ONE_TIME_DIR.is_dir():
            self.skipTest("doc/one_time_scripts/ not found")
        proc = subprocess.run(
            [sys.executable, str(CHECK_SPAN_PATH)],
            cwd=ONE_TIME_DIR,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertNotEqual(
            proc.returncode,
            0,
            "check_span.py must fail when run with cwd=doc/one_time_scripts/ (data/ not visible)",
        )


# ---------------------------------------------------------------------------
# §1 — check_span.py：空結果時會 IndexError（source 契約：目前無 df.empty 防呆）
# ---------------------------------------------------------------------------


class TestCheckSpanEmptyResultContract(unittest.TestCase):
    """Review §1: Script must guard against empty query result to avoid IndexError on df[col][0]."""

    def test_check_span_source_has_empty_df_guard(self):
        """Contract/source: check_span.py must check df.empty (or equivalent) before the print loop (Review §1)."""
        if not CHECK_SPAN_PATH.exists():
            self.skipTest("scripts/check_span.py not found")
        src = CHECK_SPAN_PATH.read_text(encoding="utf-8")
        self.assertIn(
            "df.empty",
            src,
            "Script must guard against empty result (e.g. if df.empty) before df[col][0] (Review §1).",
        )


# ---------------------------------------------------------------------------
# §2 — doc/one_time_scripts patch 腳本自錯誤 cwd 執行應失敗、不修改 trainer/
# ---------------------------------------------------------------------------


class TestOneTimeScriptsRequireRepoRoot(unittest.TestCase):
    """Review §2: Patch scripts use open('trainer/...'); from doc/one_time_scripts/ cwd they must fail."""

    def test_patch_backtester_from_one_time_cwd_fails(self):
        """Running patch_backtester.py with cwd=doc/one_time_scripts/ fails (trainer/ not there)."""
        patch_path = ONE_TIME_DIR / "patch_backtester.py"
        if not patch_path.exists():
            self.skipTest("doc/one_time_scripts/patch_backtester.py not found")
        proc = subprocess.run(
            [sys.executable, str(patch_path)],
            cwd=ONE_TIME_DIR,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertNotEqual(
            proc.returncode,
            0,
            "patch_backtester.py must fail when cwd=doc/one_time_scripts/ (trainer/ not under cwd)",
        )

    def test_trainer_backtester_unchanged_after_patch_from_wrong_cwd(self):
        """After running patch from wrong cwd, trainer/backtester.py content unchanged (no accidental write)."""
        patch_path = ONE_TIME_DIR / "patch_backtester.py"
        if not patch_path.exists() or not TRAINER_BACKTESTER.exists():
            self.skipTest("patch_backtester.py or trainer/backtester.py not found")
        content_before = TRAINER_BACKTESTER.read_bytes()
        subprocess.run(
            [sys.executable, str(patch_path)],
            cwd=ONE_TIME_DIR,
            capture_output=True,
            timeout=10,
        )
        content_after = TRAINER_BACKTESTER.read_bytes()
        self.assertEqual(
            content_before,
            content_after,
            "trainer/backtester.py must be unchanged after running patch from wrong cwd (Review §2).",
        )
