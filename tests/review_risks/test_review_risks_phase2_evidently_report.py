"""Phase 2 T8 Evidently report script — Code Review 風險點轉成最小可重現測試（tests only，不修改 production）。

對應 STATUS.md « Code Review：Phase 2 T8 變更（Evidently 腳本 + 使用說明）» §1–§4、§6。
§1：output-dir 相對路徑時相對於當前工作目錄（契約：自非 repo root 執行時報告寫入 cwd 下）。
§2：空 DataFrame（空 CSV）時腳本應失敗（return code 非 0）。
§3：輸入路徑為目錄時應 exit 1，stderr 宜含 directory/not a file（目前可能為 pandas 錯誤，鎖定 exit 1）。
§4：report.run() 拋錯時腳本應回傳非 0（目前未捕獲時 main 僅捕獲 ValueError，鎖定行為）。
§6：phase2_evidently_usage.md 宜含路徑受控／勿未信任輸入之提醒（文件契約）。

執行方式（repo 根目錄）：
  pytest tests/review_risks/test_review_risks_phase2_evidently_report.py -v
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
EVIDENTLY_USAGE_DOC = REPO_ROOT / "doc" / "phase2_evidently_usage.md"


def _run_script(
    reference: str,
    current: str,
    output_dir: str = "out/evidently_reports",
    cwd: Path | None = None,
) -> subprocess.CompletedProcess:
    """Run generate_evidently_report as subprocess; cwd=None uses current process cwd."""
    cmd = [
        sys.executable,
        "-m",
        "trainer.scripts.generate_evidently_report",
        "--reference",
        reference,
        "--current",
        current,
        "--output-dir",
        output_dir,
    ]
    return subprocess.run(
        cmd,
        cwd=cwd or REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )


# ---------------------------------------------------------------------------
# §1 — output-dir 相對路徑時相對於當前工作目錄
# ---------------------------------------------------------------------------


class TestGenerateEvidentlyReportOutputDirRelativeToCwd(unittest.TestCase):
    """Review §1: When run from a non-repo-root cwd with relative --output-dir, report is under that cwd."""

    def test_relative_output_dir_under_cwd_when_evidently_available(self):
        """From a temp cwd, relative --output-dir places report under cwd (contract)."""
        try:
            from evidently import Report  # noqa: F401
        except ImportError:
            self.skipTest("evidently not installed")

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ref_csv = tmp_path / "ref.csv"
            cur_csv = tmp_path / "cur.csv"
            ref_csv.write_text("a,b\n1,2\n")
            cur_csv.write_text("a,b\n3,4\n")

            out_subdir = tmp_path / "out" / "evidently_reports"
            ret = _run_script(
                str(ref_csv),
                str(cur_csv),
                output_dir="out/evidently_reports",
                cwd=tmp_path,
            )
            self.assertEqual(ret.returncode, 0, msg=f"stderr: {ret.stderr!r}")
            self.assertTrue(
                (out_subdir / "data_drift_report.html").exists(),
                msg="Report should be written under cwd/out/evidently_reports when cwd is not repo root (T8 §1).",
            )


# ---------------------------------------------------------------------------
# §2 — 空 DataFrame 時腳本應失敗
# ---------------------------------------------------------------------------


class TestGenerateEvidentlyReportEmptyDataFrames(unittest.TestCase):
    """Review §2: Empty reference or current (e.g. header-only CSV) should cause script to fail (non-zero exit)."""

    def test_empty_reference_csv_exits_non_zero(self):
        """When reference is header-only CSV, script should exit non-zero (or raise; lock current behavior)."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ref_csv = tmp_path / "ref.csv"
            cur_csv = tmp_path / "cur.csv"
            ref_csv.write_text("a,b\n")  # empty
            cur_csv.write_text("a,b\n1,2\n")

            ret = _run_script(str(ref_csv), str(cur_csv), cwd=tmp_path)
            self.assertNotEqual(
                ret.returncode,
                0,
                msg="Empty reference CSV should lead to non-zero exit (T8 §2).",
            )


# ---------------------------------------------------------------------------
# §3 — 輸入路徑為目錄時應 exit 1
# ---------------------------------------------------------------------------


class TestGenerateEvidentlyReportDirectoryPathFails(unittest.TestCase):
    """Review §3: When --reference or --current is a directory, script should exit 1."""

    def test_reference_is_directory_exits_one(self):
        """Passing a directory as --reference should yield exit code 1 (stderr may mention directory/not a file)."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cur_csv = tmp_path / "cur.csv"
            cur_csv.write_text("a,b\n1,2\n")

            ret = _run_script(str(tmp_path), str(cur_csv), cwd=tmp_path)
            self.assertEqual(
                ret.returncode,
                1,
                msg="--reference pointing to directory should exit 1 (T8 §3).",
            )
            # Optional: once production adds is_file() check, stderr should contain "directory" or "not a file"
            # self.assertIn("directory", ret.stderr.lower() or "not a file" in ret.stderr.lower())


# ---------------------------------------------------------------------------
# §4 — report.run() 拋錯時腳本應回傳非 0
# ---------------------------------------------------------------------------


class TestGenerateEvidentlyReportEvidentlyRunFailureReturnsNonZero(unittest.TestCase):
    """Review §4: When Evidently report.run() raises, script should return non-zero (or propagate; main catches ValueError)."""

    def test_when_report_run_raises_value_error_main_returns_one(self):
        """Mock Report.run to raise ValueError; main() catches ValueError and returns 1."""
        try:
            import evidently  # must be importable so script does not exit at ImportError
        except ImportError:
            self.skipTest("evidently not installed")

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ref_csv = tmp_path / "ref.csv"
            cur_csv = tmp_path / "cur.csv"
            ref_csv.write_text("a,b\n1,2\n")
            cur_csv.write_text("a,b\n3,4\n")

            class MockReport:
                def __init__(self, metrics=None):
                    pass

                def run(self, reference_data=None, current_data=None):
                    raise ValueError("mock Evidently failure")

            with patch.object(evidently, "Report", MockReport):
                from trainer.scripts.generate_evidently_report import main

                with patch.object(
                    sys,
                    "argv",
                    [
                        "generate_evidently_report",
                        "--reference",
                        str(ref_csv),
                        "--current",
                        str(cur_csv),
                        "--output-dir",
                        str(tmp_path / "out"),
                    ],
                ):
                    exit_code = main()
            self.assertEqual(
                exit_code,
                1,
                msg="When report.run() raises ValueError, main() should return 1 (T8 §4).",
            )


# ---------------------------------------------------------------------------
# §6 — 使用說明文件宜含路徑受控／勿未信任輸入
# ---------------------------------------------------------------------------


class TestPhase2EvidentlyUsageDocContainsControlledSourceWarning(unittest.TestCase):
    """Review §6: phase2_evidently_usage.md should mention paths as controlled source / do not use untrusted input."""

    def test_evidently_usage_doc_mentions_controlled_source_or_untrusted(self):
        """Doc should contain at least one of 受控, 勿, 未信任 (path/security context)."""
        if not EVIDENTLY_USAGE_DOC.exists():
            self.skipTest("doc/phase2_evidently_usage.md not found")
        text = EVIDENTLY_USAGE_DOC.read_text(encoding="utf-8")
        keywords = ("受控", "勿", "未信任", "敏感")
        self.assertTrue(
            any(k in text for k in keywords),
            msg="phase2_evidently_usage.md should state that paths are controlled source or do not use untrusted input (T8 §6).",
        )


if __name__ == "__main__":
    unittest.main()
