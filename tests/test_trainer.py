"""tests/test_trainer.py
========================
Unit tests for trainer/trainer.py — sample_weight correctness and artifact bundle completeness.

No ClickHouse; uses synthetic DataFrames and AST/source inspection to avoid
importing trainer (which pulls in db_conn/clickhouse_connect).
PLAN Step 10: sample_weight correctness, artifact bundle completeness.
"""

from __future__ import annotations

import ast
import pathlib
import unittest

import pandas as pd


_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_TRAINER_PATH = _REPO_ROOT / "trainer" / "trainer.py"
_TRAINER_SRC = _TRAINER_PATH.read_text(encoding="utf-8")
_TRAINER_TREE = ast.parse(_TRAINER_SRC)


def _get_func_src(name: str) -> str:
    for node in _TRAINER_TREE.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(_TRAINER_SRC, node) or ""
    return ""


# ---------------------------------------------------------------------------
# sample_weight correctness (spec: 1/N_visit per row)
# ---------------------------------------------------------------------------

def _sample_weight_spec(df: pd.DataFrame) -> pd.Series:
    """Replicate trainer.compute_sample_weights spec: weight = 1 / N_visit per (canonical_id, gaming_day)."""
    if "gaming_day" not in df.columns or "canonical_id" not in df.columns:
        return pd.Series(1.0, index=df.index)
    visit_key = df["canonical_id"].astype(str) + "_" + df["gaming_day"].astype(str)
    n_visit = visit_key.map(visit_key.value_counts())
    return (1.0 / n_visit).fillna(1.0)


class TestSampleWeightCorrectness(unittest.TestCase):
    """Test that the documented sample_weight formula (1/N_visit) is correct."""

    def test_single_visit_all_rows_same_weight(self):
        """One (canonical_id, gaming_day) → each row gets weight 1/N."""
        df = pd.DataFrame({
            "canonical_id": ["P1", "P1", "P1"],
            "gaming_day": ["2025-01-01", "2025-01-01", "2025-01-01"],
        })
        sw = _sample_weight_spec(df)
        self.assertEqual(len(sw), 3)
        self.assertAlmostEqual(sw.iloc[0], 1.0 / 3.0)
        self.assertAlmostEqual(sw.iloc[1], 1.0 / 3.0)
        self.assertAlmostEqual(sw.iloc[2], 1.0 / 3.0)

    def test_two_visits_weights_sum_to_one_per_visit(self):
        """Weights per visit sum to 1.0 (each visit contributes equally to loss)."""
        df = pd.DataFrame({
            "canonical_id": ["P1", "P1", "P2"],
            "gaming_day": ["2025-01-01", "2025-01-01", "2025-01-01"],
        })
        sw = _sample_weight_spec(df)
        self.assertAlmostEqual(sw.iloc[0], 0.5)
        self.assertAlmostEqual(sw.iloc[1], 0.5)
        self.assertAlmostEqual(sw.iloc[2], 1.0)

    def test_trainer_compute_sample_weights_implements_spec(self):
        """trainer.compute_sample_weights source implements visit_key and 1/n_visit."""
        src = _get_func_src("compute_sample_weights")
        self.assertIn("visit_key", src)
        self.assertIn("value_counts", src)
        self.assertTrue(
            "1.0" in src and ("/ n_visit" in src or "/n_visit" in src),
            "compute_sample_weights should use 1/N_visit",
        )


# ---------------------------------------------------------------------------
# get_model_version — format
# ---------------------------------------------------------------------------

class TestGetModelVersion(unittest.TestCase):
    def test_model_version_format_in_source(self):
        """get_model_version returns YYYYMMDD-HHMMSS-<suffix> per docstring."""
        src = _get_func_src("get_model_version")
        self.assertIn("strftime", src)
        self.assertIn("%Y%m%d", src)
        self.assertIn("%H%M%S", src)


# ---------------------------------------------------------------------------
# save_artifact_bundle — writes required files
# ---------------------------------------------------------------------------

class TestArtifactBundleCompleteness(unittest.TestCase):
    def test_save_artifact_bundle_writes_rated_and_nonrated_pkl(self):
        """save_artifact_bundle must write rated_model.pkl and nonrated_model.pkl."""
        src = _get_func_src("save_artifact_bundle")
        self.assertIn("rated_model.pkl", src)
        self.assertIn("nonrated_model.pkl", src)

    def test_save_artifact_bundle_writes_model_version_and_feature_list(self):
        """save_artifact_bundle must write model_version and feature_list.json."""
        src = _get_func_src("save_artifact_bundle")
        self.assertIn("model_version", src)
        self.assertIn("feature_list.json", src)

    def test_save_artifact_bundle_writes_legacy_walkaway_pkl(self):
        """save_artifact_bundle must write walkaway_model.pkl for backward compat."""
        src = _get_func_src("save_artifact_bundle")
        self.assertIn("walkaway_model.pkl", src)


if __name__ == "__main__":
    unittest.main()
