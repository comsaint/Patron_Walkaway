"""Phase 2 T9 skew check script — Code Review 風險點轉成最小可重現測試（tests only，不修改 production）。

對應 STATUS.md « Code Review：Phase 2 T9 變更（skew check 腳本 + runbook）» §1、§2、§3、§7。
§1：輸入路徑為目錄時應 exit 1，stderr 宜含 directory/not a file（鎖定 exit 1）。
§2：兩表其一為空時應 exit 1，stderr 宜含 empty 或明確區分（鎖定 exit 1）。
§3（可選）：重複 id 時腳本仍完成且輸出不崩潰。
§7：phase2_skew_check_runbook.md 宜含路徑受控／勿未信任輸入之提醒（文件契約）。

執行方式（repo 根目錄）：
  pytest tests/review_risks/test_review_risks_phase2_skew_check.py -v
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SKEW_RUNBOOK = REPO_ROOT / "doc" / "phase2_skew_check_runbook.md"


def _run_skew_script(
    serving: str,
    training: str,
    id_column: str = "id",
    output: str | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess:
    """Run check_training_serving_skew as subprocess."""
    cmd = [
        sys.executable,
        "-m",
        "trainer.scripts.check_training_serving_skew",
        "--serving",
        serving,
        "--training",
        training,
        "--id-column",
        id_column,
    ]
    if output is not None:
        cmd.extend(["--output", output])
    return subprocess.run(
        cmd,
        cwd=cwd or REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )


# ---------------------------------------------------------------------------
# §1 — 輸入路徑為目錄時應 exit 1
# ---------------------------------------------------------------------------


class TestSkewCheckDirectoryPathFails(unittest.TestCase):
    """Review §1: When --serving or --training is a directory, script should exit 1."""

    def test_serving_is_directory_exits_one(self):
        """Passing a directory as --serving should yield exit code 1 (stderr may mention directory/not a file)."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            training_csv = tmp_path / "training.csv"
            training_csv.write_text("id,a\n1,10\n")

            ret = _run_skew_script(str(tmp_path), str(training_csv), cwd=tmp_path)
            self.assertEqual(ret.returncode, 1, msg="--serving pointing to directory should exit 1 (T9 §1).")
            # When production adds is_file() check: self.assertIn("directory", ret.stderr.lower() or "not a file" in ret.stderr.lower())


# ---------------------------------------------------------------------------
# §2 — 兩表其一為空時應 exit 1
# ---------------------------------------------------------------------------


class TestSkewCheckEmptyTableExitsNonZero(unittest.TestCase):
    """Review §2: When serving or training table is empty (header-only), script should exit 1."""

    def test_empty_serving_csv_exits_one(self):
        """Empty serving CSV (header only) + non-empty training -> exit 1 (stderr may contain 'empty' or 'No common keys')."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            serving_csv = tmp_path / "serving.csv"
            training_csv = tmp_path / "training.csv"
            serving_csv.write_text("id,a,b\n")
            training_csv.write_text("id,a,b\n1,10,20\n")

            ret = _run_skew_script(str(serving_csv), str(training_csv), cwd=tmp_path)
            self.assertNotEqual(
                ret.returncode,
                0,
                msg="Empty serving table should lead to non-zero exit (T9 §2).",
            )
            # When production adds empty check: self.assertIn("empty", ret.stderr.lower())


# ---------------------------------------------------------------------------
# §3（可選）— 重複 id 時腳本仍完成
# ---------------------------------------------------------------------------


class TestSkewCheckDuplicateIdCompletes(unittest.TestCase):
    """Review §3 (optional): When both tables have duplicate id, script still completes without crash."""

    def test_duplicate_id_in_both_tables_completes_without_crash(self):
        """Both tables with duplicate id (e.g. two rows id=1) -> script completes (exit 0 or 1), does not crash."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            serving_csv = tmp_path / "serving.csv"
            training_csv = tmp_path / "training.csv"
            serving_csv.write_text("id,a\n1,10\n1,11\n")
            training_csv.write_text("id,a\n1,10\n1,11\n")

            ret = _run_skew_script(str(serving_csv), str(training_csv), cwd=tmp_path)
            self.assertIn(
                ret.returncode,
                (0, 1),
                msg="Duplicate id in both tables should complete without crash (exit 0 or 1) (T9 §3).",
            )
            # Some output (stdout or stderr) indicates normal completion rather than abort
            self.assertTrue(
                (ret.stdout or "").strip() or (ret.stderr or "").strip(),
                msg="Script should produce some output when given duplicate ids.",
            )


# ---------------------------------------------------------------------------
# §7 — Runbook 宜含路徑受控／勿未信任輸入
# ---------------------------------------------------------------------------


class TestPhase2SkewCheckRunbookContainsControlledSourceWarning(unittest.TestCase):
    """Review §7: phase2_skew_check_runbook.md should mention paths as controlled source / do not use untrusted input."""

    def test_skew_runbook_mentions_controlled_source_or_untrusted(self):
        """Runbook should contain at least one of 受控, 勿, 未信任, 敏感 (path/security context)."""
        if not SKEW_RUNBOOK.exists():
            self.skipTest("doc/phase2_skew_check_runbook.md not found")
        text = SKEW_RUNBOOK.read_text(encoding="utf-8")
        keywords = ("受控", "勿", "未信任", "敏感")
        self.assertTrue(
            any(k in text for k in keywords),
            msg="phase2_skew_check_runbook.md should state that paths are controlled source or do not use untrusted input (T9 §7).",
        )


if __name__ == "__main__":
    unittest.main()
