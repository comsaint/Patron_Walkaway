"""tests/test_backtester.py
===========================
Unit tests for trainer/backtester.py — dual metrics and per-visit TP dedup.

No ClickHouse; uses AST/source inspection and local spec replication for
compute_micro_metrics / compute_macro_by_visit_metrics behavior.
PLAN Step 10: dual metrics, per-visit TP dedup evaluation.
"""

from __future__ import annotations

import ast
import pathlib
import unittest

import numpy as np
import pandas as pd


_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_BACKTESTER_PATH = _REPO_ROOT / "trainer" / "training" / "backtester.py"
_SRC = _BACKTESTER_PATH.read_text(encoding="utf-8")
_TREE = ast.parse(_SRC)


def _get_func_src(name: str) -> str:
    for node in _TREE.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(_SRC, node) or ""
    return ""


# ---------------------------------------------------------------------------
# Dual metrics (micro + macro) — spec replication
# ---------------------------------------------------------------------------

def _micro_metrics_spec(df: pd.DataFrame, threshold: float) -> dict:
    """Replicate backtester compute_micro_metrics (v10 single threshold)."""
    df = df.copy()
    df["is_alert"] = np.where(df["is_rated"], df["score"] >= threshold, False)
    n_alerts = int(df["is_alert"].sum())
    n_tp = int((df["is_alert"] & (df["label"] == 1)).sum())
    n_pos = int((df["label"] == 1).sum())
    prec = n_tp / n_alerts if n_alerts > 0 else 0.0
    rec = n_tp / n_pos if n_pos > 0 else 0.0
    return {"precision": prec, "recall": rec, "alerts": n_alerts, "true_alerts": n_tp, "positives": n_pos}


def _macro_by_visit_spec(df: pd.DataFrame, threshold: float) -> dict:
    """Replicate backtester compute_macro_by_gaming_day_metrics (v10 single threshold)."""
    if "gaming_day" not in df.columns or "canonical_id" not in df.columns:
        return {}
    df = df.copy()
    df["is_alert"] = np.where(df["is_rated"], df["score"] >= threshold, False)
    visit_prec_list = []
    visit_rec_list = []
    for _, grp in df.groupby(["canonical_id", "gaming_day"]):
        has_pos = int((grp["label"] == 1).sum()) > 0
        n_alerted = int(grp["is_alert"].sum())
        has_tp = int((grp["is_alert"] & (grp["label"] == 1)).any())
        if n_alerted > 0:
            visit_prec_list.append(has_tp / n_alerted)
        if has_pos:
            visit_rec_list.append(float(has_tp))
    return {
        "macro_precision": float(np.mean(visit_prec_list)) if visit_prec_list else 0.0,
        "macro_recall": float(np.mean(visit_rec_list)) if visit_rec_list else 0.0,
    }


class TestDualMetrics(unittest.TestCase):
    """Dual metrics: micro (observation-level) and macro-by-visit."""

    def test_micro_metrics_precision_recall(self):
        """Micro: precision = TP/alerts, recall = TP/positives."""
        df = pd.DataFrame({
            "score": [0.9, 0.1, 0.8],
            "label": [1, 0, 1],
            "is_rated": [True, True, False],
        })
        out = _micro_metrics_spec(df, threshold=0.5)
        # v10: single rated model scores all rows; is_rated is tracked but all rows are scored
        self.assertEqual(out["alerts"], 1)
        self.assertEqual(out["true_alerts"], 1)
        self.assertEqual(out["positives"], 2)
        self.assertAlmostEqual(out["precision"], 1.0)
        self.assertAlmostEqual(out["recall"], 0.5)

    def test_macro_per_visit_at_most_one_tp(self):
        """Macro: one visit with 3 alerted TP rows counts as 1 TP for that visit."""
        df = pd.DataFrame({
            "canonical_id": ["P1", "P1", "P1"],
            "gaming_day": ["2025-01-01", "2025-01-01", "2025-01-01"],
            "score": [0.9, 0.85, 0.8],
            "label": [1, 1, 1],
            "is_rated": [True, True, True],
        })
        out = _macro_by_visit_spec(df, threshold=0.5)
        # One visit, 3 alerts, 3 positives, but at most 1 TP per visit → prec = 1/3? No: has_tp = 1 (any), so per-visit prec = 1/1 = 1 (one visit, 3 alerts, has_tp=1 → prec for that visit = 1/3? No - the code does visit_prec_list.append(has_tp / n_alerted) so 1/3. And visit_rec_list.append(has_tp) so 1. So macro_recall = mean([1]) = 1.0. Macro_prec = mean([1/3]) = 1/3.
        self.assertAlmostEqual(out["macro_precision"], 1.0 / 3.0)
        self.assertAlmostEqual(out["macro_recall"], 1.0)

    def test_backtester_defines_compute_micro_and_macro(self):
        """backtester.py defines compute_micro_metrics and compute_macro_by_gaming_day_metrics."""
        self.assertIn("def compute_micro_metrics", _SRC)
        self.assertIn("def compute_macro_by_gaming_day_metrics", _SRC)

    def test_macro_source_uses_per_visit_dedup(self):
        """compute_macro_by_gaming_day_metrics uses per-gaming-day at-most-1-TP (has_tp = any())."""
        src = _get_func_src("compute_macro_by_gaming_day_metrics")
        self.assertIn("groupby", src)
        self.assertIn("gaming_day", src)
        self.assertTrue(
            "any()" in src or "has_tp" in src,
            "macro metrics should apply per-visit TP dedup (at most 1 TP per visit)",
        )


# ---------------------------------------------------------------------------
# Issue #12 PR-12.5 — backtester rerouted to stable module surfaces.
#
# After PR-12.5, backtester pulls data ingress, feature pipeline, and pure
# metric helpers from their owning modules instead of cross-importing from
# trainer.py. These tests freeze that boundary so future refactors don't
# regress backtester back to "import everything from trainer".
# ---------------------------------------------------------------------------


class TestBacktesterStableImports(unittest.TestCase):
    """PR-12.5: backtester imports come from the new modules."""

    def test_backtester_uses_data_sources_for_ingress(self):
        self.assertIn(
            "from trainer.training.data_sources import",
            _SRC,
            "backtester should import data ingress from data_sources",
        )
        for name in ("load_clickhouse_data", "load_local_parquet"):
            self.assertIn(name, _SRC)

    def test_backtester_uses_feature_pipeline_for_dq_and_track_human(self):
        self.assertIn(
            "from trainer.training.feature_pipeline import",
            _SRC,
            "backtester should import DQ + Track Human attach from feature_pipeline",
        )
        for name in ("apply_dq", "add_track_human_features"):
            self.assertIn(name, _SRC)

    def test_backtester_uses_metrics_eval_for_precision_helper(self):
        self.assertIn(
            "from trainer.training.metrics_eval import",
            _SRC,
            "backtester should pull pure metric helpers from metrics_eval",
        )
        # Either alias is acceptable; backtester keeps the underscore name in
        # local scope for backwards compatibility with existing callers.
        self.assertTrue(
            "precision_prod_adjusted" in _SRC,
            "backtester should import precision_prod_adjusted (or its alias) from metrics_eval",
        )

    def test_backtester_does_not_re_pull_moved_symbols_from_trainer(self):
        """Sanity: symbols already moved must not still come via trainer.py."""
        moved = (
            "load_clickhouse_data",
            "load_local_parquet",
            "apply_dq",
            "add_track_human_features",
            "_precision_prod_adjusted",
        )
        # We grep the trainer-import block and ensure the moved names are
        # absent from the import list specifically. A negative match against
        # the full file would be too strict because comments and helper logic
        # may still reference these names by string.
        marker = "from trainer.training.trainer import"
        chunks = _SRC.split(marker)
        # First chunk is everything before the first trainer import; skip it.
        for chunk in chunks[1:]:
            head = chunk.split(")", 1)[0]
            for name in moved:
                self.assertNotIn(
                    name, head,
                    f"backtester should not re-pull {name} from trainer.py "
                    "after PR-12.5; import from the owning module instead.",
                )


if __name__ == "__main__":
    unittest.main()
