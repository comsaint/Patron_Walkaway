"""
doc/plan_pipeline_diagnostics_and_mlflow_artifacts.md §6 — 靜態契約補強。

鎖定：建包 BUNDLE_FILES、MLflow bundle 迴圈檔名順序、Step7 RSS 來自 psutil.Process().memory_info().rss、
oom_precheck_step7_rss_error_ratio 與 peak／precheck 的除法關係（對齊 trainer.run_pipeline 現況）。
"""

from __future__ import annotations

import ast
import inspect
import re
import textwrap
import unittest

from package.build_deploy_package import BUNDLE_FILES
from trainer.training import trainer as trainer_mod


def _run_pipeline_src() -> str:
    return inspect.getsource(trainer_mod.run_pipeline)


def _bundle_artifact_section(src: str) -> str:
    m = re.search(
        r"# Phase 2 / pipeline plan: small-file artifacts.*?"
        r"log_metrics_safe\(mlflow_metrics\)",
        src,
        re.DOTALL,
    )
    assert m is not None, "run_pipeline must contain bundle block + log_metrics_safe(mlflow_metrics)"
    return m.group(0)


def _count_log_artifact_safe_calls_in_run_pipeline_ast() -> int:
    """STATUS Code Review #2 MRE: count Name(...) calls, survives comment/string false positives on substring count."""
    src = textwrap.dedent(inspect.getsource(trainer_mod.run_pipeline))
    mod = ast.parse(src)
    fn = mod.body[0]
    if not isinstance(fn, ast.FunctionDef) or fn.name != "run_pipeline":
        raise AssertionError("expected ast body[0] to be def run_pipeline")

    class _Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.n = 0

        def visit_Call(self, node: ast.Call) -> None:
            f = node.func
            if isinstance(f, ast.Name) and f.id == "log_artifact_safe":
                self.n += 1
            self.generic_visit(node)

    v = _Visitor()
    v.visit(fn)
    return v.n


class TestSection6BundleFiles(unittest.TestCase):
    """§6 打包：BUNDLE_FILES 含 pipeline_diagnostics.json（與缺檔 warning 單元測試呼應）。"""

    def test_pipeline_diagnostics_in_bundle_files_after_training_metrics(self):
        self.assertIn("training_metrics.json", BUNDLE_FILES)
        for f in ("training_metrics.v2.json", "feature_importance.json", "comparison_metrics.json"):
            self.assertIn(f, BUNDLE_FILES)
        self.assertIn("pipeline_diagnostics.json", BUNDLE_FILES)
        self.assertLess(
            BUNDLE_FILES.index("training_metrics.json"),
            BUNDLE_FILES.index("training_metrics.v2.json"),
        )
        self.assertLess(
            BUNDLE_FILES.index("comparison_metrics.json"),
            BUNDLE_FILES.index("pipeline_diagnostics.json"),
        )

    def test_bundle_files_filenames_unique(self):
        """STATUS Code Review #7 MRE: duplicate names would skew index() ordering checks and copy semantics."""
        self.assertEqual(
            len(BUNDLE_FILES),
            len(set(BUNDLE_FILES)),
            "BUNDLE_FILES must not list the same filename twice (deploy copy order / overwrite risk)",
        )


class TestSection6BundleArtifactChunkMre(unittest.TestCase):
    """STATUS Code Review #1 MRE: sliced chunk must still be the real bundle block, not an earlier stray log_metrics."""

    def test_chunk_contains_has_active_run_fname_loop_and_single_success_metrics_call(self):
        chunk = _bundle_artifact_section(_run_pipeline_src())
        self.assertIn("if has_active_run():", chunk)
        self.assertIn("for _fname in", chunk)
        self.assertIn("training_metrics.json", chunk)
        self.assertEqual(
            chunk.count("log_metrics_safe(mlflow_metrics)"),
            1,
            "chunk must be exactly one success-path metrics block (else slice anchor is wrong)",
        )


class TestSection6MlflowBundleFnameOrder(unittest.TestCase):
    """§6 Mock MLflow 可選項：成功路徑可能上傳的檔名與順序（is_file 守衛下逐一 log_artifact_safe）。"""

    def test_for_fname_tuple_order(self):
        chunk = _bundle_artifact_section(_run_pipeline_src())
        m = re.search(
            r"for _fname in \(\s*((?:\"[^\"]+\"\s*,\s*)+\"[^\"]+\"\s*,?)\s*\)",
            chunk,
            re.DOTALL,
        )
        self.assertIsNotNone(m, "expected for _fname in ( ... ) tuple of string literals")
        inner = m.group(1)
        names = re.findall(r'"([^"]+)"', inner)
        self.assertGreaterEqual(
            len(names),
            4,
            "bundle small-file loop should enumerate at least four artifacts",
        )
        self.assertEqual(names[0], "training_metrics.json")
        self.assertIn("pipeline_diagnostics.json", names)
        self.assertIn("model_metadata.json", names)
        self.assertIn("feature_spec.yaml", names)
        self.assertEqual(names[-1], "model_version")
        self.assertEqual(
            chunk.count("log_artifact_safe(_ap,"),
            1,
            "one log_artifact_safe call inside bundle for-loop",
        )

    def test_run_pipeline_ast_exactly_one_log_artifact_safe_call(self):
        """STATUS Code Review #2 MRE: AST count (whole run_pipeline), not fragile substring count in comments."""
        self.assertEqual(
            _count_log_artifact_safe_calls_in_run_pipeline_ast(),
            1,
            "run_pipeline should call log_artifact_safe once (bundle loop); refactor → update contract",
        )


class TestSection6RssSamplingInRunPipeline(unittest.TestCase):
    """§6 OOM／RSS：RSS 欄位由 run_pipeline 內 psutil 採樣，而非 diagnostics helper 自行推算。"""

    def test_step7_rss_start_from_process_memory_info_rss(self):
        src = _run_pipeline_src()
        self.assertRegex(
            src,
            r"step7_rss_start_gb\s*=\s*[^\n]+memory_info\(\)\.rss",
        )

    def test_step7_rss_end_from_process_memory_info_rss(self):
        src = _run_pipeline_src()
        self.assertRegex(
            src,
            r"step7_rss_end_gb\s*=\s*[^\n]+memory_info\(\)\.rss",
        )

    def test_step7_rss_start_assignment_after_checkpoint_psutil_import(self):
        """STATUS Code Review #3 MRE: start RSS line must stay tied to Step7 checkpoint import (not earlier param names)."""
        src = _run_pipeline_src()
        marker = "import psutil as _psutil  # optional dependency (best-effort)"
        i_imp = src.find(marker)
        assign = "step7_rss_start_gb = _step7_process.memory_info().rss / (1024**3)"
        i_a = src.find(assign)
        self.assertGreater(i_imp, 0, "expected Step7 best-effort psutil import comment")
        self.assertGreater(i_a, i_imp, "RSS start assignment must follow that import")

    def test_step9_rss_end_assignment_in_step9_snapshot_block(self):
        """STATUS Code Review #3 MRE: end RSS assignment must remain in Step9 snapshot block (distinct from Step7)."""
        src = _run_pipeline_src()
        anchor = "# T12.2: capture RSS/sys RAM snapshot at Step 9 end"
        i_anchor = src.find(anchor)
        assign = "step7_rss_end_gb = _proc_end.memory_info().rss / (1024**3)"
        i_a = src.find(assign)
        self.assertGreater(i_anchor, 0, "expected Step9 RSS snapshot comment anchor")
        self.assertGreater(i_a, i_anchor, "RSS end assignment must follow Step9 anchor")

    def test_step7_rss_peak_is_max_of_start_and_end(self):
        src = _run_pipeline_src()
        self.assertIsNotNone(
            re.search(
                r"step7_rss_peak_gb\s*=\s*max\(\s*step7_rss_start_gb\s*,\s*step7_rss_end_gb\s*\)",
                src,
                re.DOTALL,
            ),
            "expected max(start,end) peak assignment (formatter-tolerant)",
        )

    def test_split_frames_released_after_step9_snapshot_before_step10(self):
        """Step 9 OOM hardening: large split DataFrames should be cleared before artifact save/MLflow phases."""
        src = _run_pipeline_src()
        i_snapshot = src.find("# T12.2: capture RSS/sys RAM snapshot at Step 9 end")
        i_release = src.find("train_df = None")
        i_step10 = src.find('print("[Step 10/10] Save artifact bundle…", flush=True)')
        self.assertGreater(i_snapshot, 0, "expected Step 9 snapshot anchor")
        self.assertGreater(i_release, i_snapshot, "split-frame release should happen after Step 9 snapshot block")
        self.assertGreater(i_step10, i_release, "split-frame release should happen before Step 10 artifact save")
        window = src[i_release : i_step10]
        self.assertIn("valid_df = None", window)
        self.assertIn("test_df = None", window)
        self.assertIn("gc.collect()", window)


class TestSection6OomPrecheckRatioFormula(unittest.TestCase):
    """§6：oom_precheck_step7_rss_error_ratio 與 peak／precheck 估算一致（除法路徑）。"""

    def test_ratio_uses_peak_over_est_peak_ram(self):
        src = _run_pipeline_src()
        self.assertIn(
            "step7_rss_peak_gb / oom_precheck_est_peak_ram_gb",
            src,
        )

    def test_oom_ratio_assignment_preceded_by_positive_precheck_guard(self):
        """STATUS Code Review #6 MRE: ratio must stay guarded (avoid silent divide-by-zero refactor)."""
        src = _run_pipeline_src()
        key = "oom_precheck_step7_rss_error_ratio ="
        i = src.find(key)
        self.assertGreater(i, 0, "expected augmented assignment block for oom ratio")
        window = src[max(0, i - 900) : i]
        self.assertIn(
            "oom_precheck_est_peak_ram_gb > 0",
            window,
            "ratio assignment must remain under positive precheck guard",
        )

