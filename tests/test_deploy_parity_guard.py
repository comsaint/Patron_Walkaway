"""建包腳本可於乾淨 process 載入（PLAN § Train–Serve Parity 步驟 5 後：已移除 TRAINER_USE_LOOKBACK，訓練／評估／serving 一律使用 SCORER_LOOKBACK_HOURS）。

Run from repo root:
  python -m pytest tests/test_deploy_parity_guard.py -v
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


class TestBuildScriptLoadsInCleanProcess(unittest.TestCase):
    """STATUS Code Review §1：建包腳本應在未先 import trainer 的 process 內可正常載入。"""

    def test_build_deploy_package_help_runs_in_subprocess(self):
        """以 subprocess 執行 python -m package.build_deploy_package --help，確認可於乾淨 process 載入。"""
        result = subprocess.run(
            [sys.executable, "-m", "package.build_deploy_package", "--help"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, f"stderr: {result.stderr!r}")
        self.assertIn("output-dir", result.stdout.lower() or result.stderr.lower(), "expected --help usage")


if __name__ == "__main__":
    unittest.main()
