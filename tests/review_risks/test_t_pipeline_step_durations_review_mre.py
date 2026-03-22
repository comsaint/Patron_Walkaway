"""
STATUS.md — Code Review：T-PipelineStepDurations（2026-03-22）

將 reviewer 風險點轉成最小可重現／契約測試；**不修改 production**。
對應 STATUS 小節各編號（#1–#6）。

所有測試僅讀取 `trainer/training/trainer.py` 文字或純 Python 模擬，**不依賴匯入 trainer 模組**（避免冷啟動過慢）。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TRAINER_PY = _REPO_ROOT / "trainer" / "training" / "trainer.py"


def _trainer_text() -> str:
    return _TRAINER_PY.read_text(encoding="utf-8")


class TestReview1MlflowMetricsRatedMergeCollisionMre(unittest.TestCase):
    """#1：`mlflow_metrics.update(_rated)` 會覆寫已存在的 step 耗時鍵（dict 語意 MRE）。"""

    def test_merge_order_overwrites_pipeline_step_keys_when_rated_collides(self):
        """鏡像 trainer 成功路徑順序：先放 pipeline 計時，再 `update(rated)`。

        若 `rated` 含同名鍵，**最終** dict 為 rated 的值 — reviewer 指出的靜默錯位風險。
        """
        total_sec = 100.0
        wall_step1 = 1.25
        mlflow_metrics: dict = {
            "total_duration_sec": total_sec,
            "step1_duration_sec": wall_step1,
            "step2_duration_sec": 2.0,
        }
        rated = {"step1_duration_sec": 999.0, "train_auc": 0.9}
        mlflow_metrics.update(rated)
        self.assertEqual(
            mlflow_metrics["step1_duration_sec"],
            999.0,
            "MRE: rated wins on key collision — pipeline wall time lost unless reassigned after update",
        )
        self.assertEqual(mlflow_metrics["train_auc"], 0.9)

    def test_run_pipeline_success_metrics_block_has_update_rated_before_log_metrics(self):
        """契約：成功路徑仍含 `update(_rated)` 且在其後呼叫 `log_metrics_safe(mlflow_metrics)`。"""
        src = _trainer_text()
        i_metrics = src.find("mlflow_metrics: dict[str, Any] = {")
        self.assertGreater(i_metrics, 0, "expected mlflow_metrics dict block in trainer.py")
        chunk = src[i_metrics:]
        i_update = chunk.find("mlflow_metrics.update(_rated)")
        i_log = chunk.find("log_metrics_safe(mlflow_metrics)")
        self.assertGreater(i_update, 0, "expected mlflow_metrics.update(_rated) after metrics dict")
        self.assertGreater(i_log, i_update, "expected log_metrics_safe after update(_rated)")


class TestReview2Step1DurationScopeContract(unittest.TestCase):
    """#2：`step1_duration_sec` 不包含 `--recent-chunks` 裁剪（原始碼順序契約）。"""

    def test_step1_duration_assignment_before_recent_chunks_slice(self):
        src = _trainer_text()
        needle_step1 = "step1_duration_sec = _el"
        needle_trim = "chunks = chunks[-recent_chunks:]"
        i1 = src.find(needle_step1)
        i2 = src.find(needle_trim)
        self.assertGreater(i1, 0, f"missing {needle_step1!r} in trainer.py")
        self.assertGreater(i2, 0, f"missing {needle_trim!r} in trainer.py")
        self.assertLess(
            i1,
            i2,
            "contract: Step 1 wall time must be recorded before recent-chunks trim",
        )


class TestReview3Step8OptionalInDiagnosticsJson(unittest.TestCase):
    """#3：Step 8 可為 None；writer 以 `if v is not None` 省略鍵（靜態契約 + 行為 MRE）。"""

    def test_writer_uses_none_omission_filter(self):
        src = _trainer_text()
        i0 = src.find("def _write_pipeline_diagnostics_json(")
        self.assertGreater(i0, 0)
        chunk = src[i0 : i0 + 6000]
        self.assertIn(
            "out = {k: v for k, v in payload.items() if v is not None}",
            chunk,
            "contract: None step durations must be omitted from pipeline_diagnostics.json",
        )
        self.assertIn('"step8_duration_sec": step8_duration_sec', chunk)

    def test_none_omission_mirrors_writer_filter(self):
        """MRE：與 writer 相同之 dict 理解，step8=None 不進入輸出。"""
        payload = {
            "step7_duration_sec": 1.0,
            "step8_duration_sec": None,
            "step9_duration_sec": 2.0,
        }
        out = {k: v for k, v in payload.items() if v is not None}
        self.assertNotIn("step8_duration_sec", out)
        self.assertIn("step7_duration_sec", out)
        self.assertIn("step9_duration_sec", out)


class TestReview4FailurePathNoStepDurationParams(unittest.TestCase):
    """#4：FAILED 路徑的 `failure_params` 不含各步耗時鍵（現狀契約）。"""

    def test_failure_params_block_excludes_step_duration_keys(self):
        src = _trainer_text()
        anchor = "# T12 failure diagnostics"
        i0 = src.find(anchor)
        self.assertGreater(i0, 0, "expected T12 failure diagnostics anchor")
        chunk = src[i0 : i0 + 3500]
        self.assertIn("failure_params = {", chunk)
        self.assertNotIn(
            "step1_duration_sec",
            chunk,
            "contract: failure post-mortem params should not yet log per-step durations",
        )
        for n in range(2, 11):
            self.assertNotIn(f"step{n}_duration_sec", chunk, f"unexpected step{n}_duration_sec in failure block")


class TestReview5DiagnosticsWriterBoundedStepKeys(unittest.TestCase):
    """#5：診斷 writer 僅固定十個 step 鍵，無無界擴張。"""

    def test_write_payload_lists_step1_through_step10_once_each(self):
        src = _trainer_text()
        i0 = src.find("def _write_pipeline_diagnostics_json(")
        self.assertGreater(i0, 0, "expected _write_pipeline_diagnostics_json in trainer.py")
        chunk = src[i0 : i0 + 8000]
        for n in range(1, 11):
            pat = f'"step{n}_duration_sec": step{n}_duration_sec'
            self.assertEqual(
                chunk.count(pat),
                1,
                f"expected exactly one payload mapping for step{n}_duration_sec in writer body",
            )


class TestReview6StepDurationKeysNoPathOrSecretPattern(unittest.TestCase):
    """#6：step 耗時鍵為數值語意；契約上 payload 鍵名符合固定 pattern（非任意字串）。"""

    def test_step_duration_keys_match_snake_case_pattern(self):
        src = _trainer_text()
        i0 = src.find("def _write_pipeline_diagnostics_json(")
        self.assertGreater(i0, 0)
        chunk = src[i0 : i0 + 8000]
        m = re.findall(r'"((?:step\d+_duration_sec))":', chunk)
        self.assertEqual(len(m), 10)
        for n, key in enumerate(m, start=1):
            self.assertEqual(key, f"step{n}_duration_sec")
