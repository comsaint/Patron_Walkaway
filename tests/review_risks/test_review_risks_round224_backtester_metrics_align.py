"""Round 224 Review — Backtester metrics align: convert reviewer risk points to tests.

Tests document desired behavior per STATUS.md Round 224 Review. Production is not
modified; tests that assert the recommended fixes use @unittest.expectedFailure
until production is updated.

Reference: PLAN.md § Backtester 評估輸出格式對齊 trainer, DECISION_LOG DEC-009/010/021.
"""

from __future__ import annotations

import inspect
import unittest

import numpy as np
import pandas as pd

import trainer.backtester as backtester_mod


# Trainer-style flat keys (Round 224 Review §1): same set for empty/zeroed returns.
# Round 229: add precision-at-recall (PLAN § Backtester precision-at-recall).
_EXPECTED_FLAT_KEYS = frozenset({
    "test_ap",
    "test_precision",
    "test_precision_prod_adjusted",
    "test_precision_prod_adjusted_reason_code",
    "test_recall",
    "test_f1",
    "test_fbeta_05",
    "threshold",
    "test_samples",
    "test_positives",
    "test_random_ap",
    "alerts",
    "alerts_per_hour",
    "test_precision_at_recall_0.001",
    "test_precision_at_recall_0.001_reason_code",
    "test_precision_at_recall_0.001_prod_adjusted",
    "test_precision_at_recall_0.001_prod_adjusted_reason_code",
    "test_precision_at_recall_0.01",
    "test_precision_at_recall_0.01_reason_code",
    "test_precision_at_recall_0.01_prod_adjusted",
    "test_precision_at_recall_0.01_prod_adjusted_reason_code",
    "test_precision_at_recall_0.1",
    "test_precision_at_recall_0.1_reason_code",
    "test_precision_at_recall_0.1_prod_adjusted",
    "test_precision_at_recall_0.1_prod_adjusted_reason_code",
    "test_precision_at_recall_0.5",
    "test_precision_at_recall_0.5_reason_code",
    "test_precision_at_recall_0.5_prod_adjusted",
    "test_precision_at_recall_0.5_prod_adjusted_reason_code",
    "threshold_at_recall_0.001",
    "threshold_at_recall_0.01",
    "threshold_at_recall_0.1",
    "threshold_at_recall_0.5",
    "alerts_per_minute_at_recall_0.001",
    "alerts_per_minute_at_recall_0.01",
    "alerts_per_minute_at_recall_0.1",
    "alerts_per_minute_at_recall_0.5",
})
# Section = flat metrics + rated_threshold + field-test alert-density audit (alerts/h plan).
_EXPECTED_SECTION_KEYS = _EXPECTED_FLAT_KEYS | {
    "rated_threshold",
    "min_alerts_per_hour_objective",
    "alerts_per_hour_meets_objective",
}


class TestR224_1_EmptyRatedSubFlatKeys(unittest.TestCase):
    """R224 Review #1: empty rated_sub should yield full flat structure to avoid KeyError."""

    def test_compute_micro_metrics_empty_df_returns_flat_keys_with_zeros(self):
        """Empty df should return full trainer-style keys with zeros, not {}."""
        out = backtester_mod.compute_micro_metrics(
            pd.DataFrame(),
            threshold=0.5,
            window_hours=1.0,
        )
        self.assertEqual(set(out.keys()), _EXPECTED_FLAT_KEYS, "Same keys as non-empty path")
        self.assertEqual(out["test_ap"], 0.0)
        self.assertEqual(out["test_precision"], 0.0)
        self.assertEqual(out["test_recall"], 0.0)
        self.assertEqual(out["test_f1"], 0.0)
        self.assertEqual(out["test_fbeta_05"], 0.0)
        self.assertEqual(out["threshold"], 0.5)
        self.assertEqual(out["test_samples"], 0)
        self.assertEqual(out["test_positives"], 0)
        self.assertEqual(out["test_random_ap"], 0.0)
        self.assertEqual(out["alerts"], 0)
        self.assertEqual(out["alerts_per_hour"], 0.0, "window_hours=1.0 → 0/1.0")
        self.assertIsNone(out["test_precision_prod_adjusted"], "empty → None prod_adjusted headline")
        self.assertEqual(out["test_precision_prod_adjusted_reason_code"], "empty_subset")
        for r in (0.001, 0.01, 0.1, 0.5):
            self.assertIsNone(out[f"test_precision_at_recall_{r}"], "empty → None (PLAN DEC-026)")
            self.assertEqual(out[f"test_precision_at_recall_{r}_reason_code"], "empty_subset")
            self.assertIsNone(
                out[f"test_precision_at_recall_{r}_prod_adjusted"],
                "empty → None prod_adjusted @recall",
            )
            self.assertEqual(
                out[f"test_precision_at_recall_{r}_prod_adjusted_reason_code"],
                "empty_subset",
            )

    def test_compute_section_metrics_empty_rated_sub_returns_micro_with_flat_keys(self):
        """Section is flat (no 'micro' nest); downstream reads out['test_ap'] (PLAN step 3)."""
        labeled = pd.DataFrame({"label": [0], "is_rated": [False], "score": [0.0]})
        rated_sub = pd.DataFrame()
        out = backtester_mod._compute_section_metrics(
            labeled=labeled,
            rated_sub=rated_sub,
            threshold=0.5,
            window_hours=1.0,
        )
        self.assertNotIn("micro", out, "PLAN step 3: section is flat, no micro nest")
        self.assertIn("rated_threshold", out)
        self.assertEqual(out["rated_threshold"], 0.5)
        self.assertTrue(
            _EXPECTED_FLAT_KEYS.issubset(set(out.keys())),
            "Section must contain all trainer-style flat keys",
        )
        self.assertIn("test_ap", out)
        self.assertEqual(out["test_ap"], 0.0)


class TestR224_2_NaNLabelsSafeStructure(unittest.TestCase):
    """R224 Review #2: label containing NaN should be guarded and return safe structure."""

    def test_compute_micro_metrics_nan_labels_returns_safe_structure(self):
        """With NaN in label, should not raise and should return zeroed flat structure (same as empty)."""
        df = pd.DataFrame({
            "score": [0.5],
            "label": [np.nan],
            "is_rated": [True],
        })
        out = backtester_mod.compute_micro_metrics(df, threshold=0.5, window_hours=1.0)
        self.assertEqual(set(out.keys()), _EXPECTED_FLAT_KEYS)
        self.assertEqual(out["test_ap"], 0.0)
        self.assertEqual(out["test_samples"], 0, "NaN label → same zeroed structure as empty")
        self.assertEqual(out["test_positives"], 0)
        self.assertIsNone(out["test_precision_prod_adjusted"])
        self.assertEqual(out["test_precision_prod_adjusted_reason_code"], "invalid_input_nan")
        for r in (0.001, 0.01, 0.1, 0.5):
            self.assertIsNone(out[f"test_precision_at_recall_{r}"], "NaN labels → None (PLAN DEC-026)")
            self.assertEqual(out[f"test_precision_at_recall_{r}_reason_code"], "invalid_input_nan")
            self.assertIsNone(out[f"test_precision_at_recall_{r}_prod_adjusted"])
            self.assertEqual(
                out[f"test_precision_at_recall_{r}_prod_adjusted_reason_code"],
                "invalid_input_nan",
            )


class TestR224_3_AllPositiveAPBehavior(unittest.TestCase):
    """R224 Review #3: all-positive labels — optional AP=0 alignment with trainer."""

    def test_compute_micro_metrics_all_positive_labels_ap_behavior(self):
        """Single-class (all positive) should yield test_ap=0.0 and None precision@recall (PLAN)."""
        df = pd.DataFrame({
            "score": [0.9, 0.8],
            "label": [1, 1],
            "is_rated": [True, True],
        })
        out = backtester_mod.compute_micro_metrics(df, threshold=0.5, window_hours=1.0)
        self.assertEqual(set(out.keys()), _EXPECTED_FLAT_KEYS)
        self.assertEqual(out["test_ap"], 0.0, "Single-class AP should be 0 (trainer-aligned)")
        self.assertIsNone(out["test_precision_prod_adjusted"])
        self.assertEqual(out["test_precision_prod_adjusted_reason_code"], "single_class")
        for r in (0.001, 0.01, 0.1, 0.5):
            self.assertIsNone(out[f"test_precision_at_recall_{r}"], "single-class → None (PLAN DEC-026)")
            self.assertEqual(out[f"test_precision_at_recall_{r}_reason_code"], "single_class")
            self.assertIsNone(out[f"test_precision_at_recall_{r}_prod_adjusted"])
            self.assertEqual(
                out[f"test_precision_at_recall_{r}_prod_adjusted_reason_code"],
                "single_class",
            )


class TestR224_4_ModuleDocNoMacroByVisit(unittest.TestCase):
    """R224 Review #4: module docstring should not reference Macro-by-visit (PLAN step 4)."""

    def test_backtester_module_doc_should_not_reference_macro_by_visit(self):
        """Module doc must not mention macro_by_visit / Macro-by-visit after PLAN step 4."""
        doc = inspect.getdoc(backtester_mod) or ""
        self.assertNotIn(
            "macro_by_visit",
            doc,
            "Module doc should not reference macro_by_visit (removed in Round 224).",
        )
        self.assertNotIn(
            "Macro-by-visit",
            doc,
            "Module doc should not reference Macro-by-visit (PLAN step 4).",
        )


class TestR224_5_SectionThresholdKeysContract(unittest.TestCase):
    """R224 Review #5 / PLAN step 3: section is flat (rated_threshold + threshold at top level)."""

    def test_compute_section_metrics_returns_rated_threshold_and_micro_with_threshold(self):
        """Section is flat: rated_threshold and threshold at top level (no micro nest)."""
        df = pd.DataFrame({
            "score": [0.6],
            "label": [1],
            "is_rated": [True],
        })
        out = backtester_mod._compute_section_metrics(
            labeled=df,
            rated_sub=df,
            threshold=0.5,
            window_hours=1.0,
        )
        self.assertNotIn("micro", out, "PLAN step 3: no micro nest")
        self.assertIn("rated_threshold", out)
        self.assertEqual(out["rated_threshold"], 0.5)
        self.assertIn("threshold", out)
        self.assertEqual(out["threshold"], 0.5)

    def test_compute_section_metrics_section_keys_exactly_expected(self):
        """Round 226 Review #2: section has exactly expected keys (no drift)."""
        df = pd.DataFrame({
            "score": [0.6],
            "label": [1],
            "is_rated": [True],
        })
        out = backtester_mod._compute_section_metrics(
            labeled=df,
            rated_sub=df,
            threshold=0.5,
            window_hours=1.0,
        )
        self.assertEqual(
            set(out.keys()),
            _EXPECTED_SECTION_KEYS,
            "Section must have exactly trainer flat keys + rated_threshold (no extra keys).",
        )

    def test_compute_section_metrics_window_hours_none_alerts_per_hour_is_none(self):
        """Round 226 Review #3: when window_hours is None, alerts_per_hour is None."""
        df = pd.DataFrame({
            "score": [0.5],
            "label": [0],
            "is_rated": [True],
        })
        out = backtester_mod._compute_section_metrics(
            labeled=df,
            rated_sub=df,
            threshold=0.5,
            window_hours=None,
        )
        self.assertIsNone(
            out.get("alerts_per_hour"),
            "alerts_per_hour must be None when window_hours is None.",
        )
        self.assertIsNone(
            out.get("alerts_per_hour_meets_objective"),
            "Cannot judge 50/h objective without alerts_per_hour.",
        )
        self.assertGreater(float(out["min_alerts_per_hour_objective"]), 0.0)


if __name__ == "__main__":
    unittest.main()
