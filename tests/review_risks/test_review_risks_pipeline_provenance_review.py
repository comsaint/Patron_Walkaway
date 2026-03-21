"""
STATUS.md「Code Review：§5 Provenance」風險點 → 最小可重現／契約測試（僅 tests）。

對應 STATUS.md §1–§2、§5–§6（§4 依 review 不強制自動化；§7 無測試）。
"""

from __future__ import annotations

import inspect
import pathlib
import re
import unittest

import pytest

from trainer.training import trainer as trainer_mod

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_PROVENANCE_SCHEMA = _REPO_ROOT / "doc" / "phase2_provenance_schema.md"
_PLAN_PIPELINE = _REPO_ROOT / "doc" / "plan_pipeline_diagnostics_and_mlflow_artifacts.md"


def _run_pipeline_src() -> str:
    return inspect.getsource(trainer_mod.run_pipeline)


def _artifact_dir_with_empty_path_name() -> str | None:
    """Return a string path where Path(path).name == '' on this OS, or None."""
    for p in (pathlib.Path("/"), pathlib.Path("C:/")):
        try:
            if p.name == "":
                return str(p)
        except (OSError, ValueError):
            continue
    return None


# ---------------------------------------------------------------------------
# STATUS Code Review §1：provenance 早於 pipeline_diagnostics 寫入（原始碼順序 MRE）
# ---------------------------------------------------------------------------


class TestReviewerR1ProvenanceBeforeDiagnosticsWrite(unittest.TestCase):
    """若未來將 provenance 移到寫檔之後，請更新此契約並同步 STATUS §1。"""

    def test_run_pipeline_calls_provenance_before_write_pipeline_diagnostics_json(self):
        src = _run_pipeline_src()
        i_prov = src.find("_log_training_provenance_to_mlflow")
        i_write = src.find("_write_pipeline_diagnostics_json")
        self.assertNotEqual(i_prov, -1, "run_pipeline should call _log_training_provenance_to_mlflow")
        self.assertNotEqual(i_write, -1, "run_pipeline should call _write_pipeline_diagnostics_json")
        self.assertLess(
            i_prov,
            i_write,
            "MRE/contract: MLflow params may be logged before pipeline_diagnostics.json exists on disk; "
            "if production is reordered (STATUS §1 fix A), flip this assertion.",
        )


# ---------------------------------------------------------------------------
# STATUS Code Review §2：artifact_dir 最後一層為空 → rel_path 異常（MRE）
# ---------------------------------------------------------------------------


def test_reviewer_r2_empty_basename_pipeline_diagnostics_rel_path_mre():
    """當 Path(artifact_dir).name == '' 時，目前實作會產生前導 '/' 的 rel（reviewer 標記風險）。"""
    ad = _artifact_dir_with_empty_path_name()
    if ad is None:
        pytest.skip("no platform path with empty Path().name")

    from contextlib import nullcontext
    from unittest.mock import patch

    with patch.object(trainer_mod, "safe_start_run") as mock_start:
        with patch.object(trainer_mod, "log_params_safe") as mock_log:
            mock_start.return_value = nullcontext()
            trainer_mod._log_training_provenance_to_mlflow(
                model_version="v1",
                artifact_dir=ad,
                training_window_start="2026-01-01",
                training_window_end="2026-01-07",
                feature_spec_path="spec.yaml",
                training_metrics_path=str(pathlib.Path(ad) / "training_metrics.json"),
                git_commit="abc",
            )
            mock_log.assert_called_once()
            (params,) = mock_log.call_args[0]

    # Current production: f"{Path(ad).name}/..." → "/pipeline_diagnostics.json"
    assert params["pipeline_diagnostics_rel_path"] == "/pipeline_diagnostics.json"


# ---------------------------------------------------------------------------
# STATUS Code Review §5：寫檔失敗時 artifact 迴圈依 is_file 略過（原始碼契約）
# ---------------------------------------------------------------------------


class TestReviewerR5BundleUploadGuardsOnIsFile(unittest.TestCase):
    """若 write 失敗且檔案不存在，不應無條件 log_artifact_safe pipeline_diagnostics。"""

    def test_small_file_bundle_loop_uses_is_file_before_log_artifact_safe(self):
        src = _run_pipeline_src()
        m = re.search(
            r"# Phase 2 / pipeline plan: small-file artifacts.*?"
            r"log_artifact_safe\(_ap, artifact_path=_bundle_artifact_path\)",
            src,
            re.DOTALL,
        )
        self.assertIsNotNone(m, "run_pipeline must contain small-file bundle + log_artifact_safe(_ap, ...)")
        chunk = m.group(0)
        self.assertIn('"pipeline_diagnostics.json"', chunk)
        self.assertIn("if _ap.is_file():", chunk)
        self.assertLess(chunk.find("if _ap.is_file():"), chunk.find("log_artifact_safe(_ap"))


# ---------------------------------------------------------------------------
# STATUS Code Review §6：文件同時提及 bundle/ 與本機慣例（低脆度契約）
# ---------------------------------------------------------------------------


class TestReviewerR6DocsMentionBundlePrefix(unittest.TestCase):
    def test_phase2_provenance_schema_mentions_bundle_prefix(self):
        text = _PROVENANCE_SCHEMA.read_text(encoding="utf-8")
        self.assertIn("bundle/", text)

    def test_plan_pipeline_doc_mentions_bundle_prefix(self):
        text = _PLAN_PIPELINE.read_text(encoding="utf-8")
        self.assertIn("bundle/", text)

