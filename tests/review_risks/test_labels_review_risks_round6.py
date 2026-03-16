"""Round 6 guardrail tests for labels.py review findings (R12-R16).

User request in this round is tests-only:
- R12: null canonical_id should be dropped (currently not enforced)
- R13/R16: tight extended_end should emit warning mentioning LABEL_LOOKAHEAD_MIN
- R14: extra input columns should be preserved
- R15: all-null payout / all-censored edge cases
"""

from __future__ import annotations

import importlib
import pathlib
import sys
import unittest
from datetime import datetime, timedelta

import pandas as pd

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _import_labels():
    repo_root_str = str(_REPO_ROOT)
    if repo_root_str not in sys.path:
        sys.path.insert(0, repo_root_str)
    return importlib.import_module("trainer.labels")


LABELS = _import_labels()
compute_labels = LABELS.compute_labels

try:
    from trainer.config import LABEL_LOOKAHEAD_MIN, WALKAWAY_GAP_MIN
except Exception:
    LABEL_LOOKAHEAD_MIN = 45
    WALKAWAY_GAP_MIN = 30


class TestLabelsReviewRisksRound6(unittest.TestCase):
    def test_r12_null_canonical_id_rows_are_dropped(self):
        base = datetime(2025, 1, 1)
        df = pd.DataFrame(
            [
                {"canonical_id": "P1", "bet_id": 1, "payout_complete_dtm": base},
                {"canonical_id": None, "bet_id": 2, "payout_complete_dtm": base + timedelta(minutes=1)},
            ]
        )
        window_end = base + timedelta(hours=1)
        extended_end = base + timedelta(days=1)

        result = compute_labels(df, window_end, extended_end)
        self.assertEqual(len(result), 1)
        self.assertFalse(result["canonical_id"].isna().any())
        self.assertEqual(result.iloc[0]["bet_id"], 1)

    def test_r13_r16_tight_extended_end_emits_warning(self):
        base = datetime(2025, 1, 1)
        df = pd.DataFrame(
            [
                {"canonical_id": "P1", "bet_id": 1, "payout_complete_dtm": base},
            ]
        )
        window_end = base + timedelta(hours=1)
        extended_end = window_end + timedelta(minutes=1)

        with self.assertLogs("trainer.labels", level="WARNING") as cm:
            compute_labels(df, window_end, extended_end)
        self.assertTrue(
            any("LABEL_LOOKAHEAD_MIN" in msg for msg in cm.output),
            msg="\n".join(cm.output),
        )
        self.assertLess(
            (extended_end - window_end).total_seconds() / 60.0,
            LABEL_LOOKAHEAD_MIN,
        )

    def test_r14_extra_columns_preserved_in_output(self):
        base = datetime(2025, 1, 1)
        df = pd.DataFrame(
            [
                {
                    "canonical_id": "P1",
                    "bet_id": 1,
                    "payout_complete_dtm": base,
                    "wager": 100.0,
                    "status": "LOSE",
                    "table_id": "T1",
                }
            ]
        )
        window_end = base + timedelta(hours=1)
        extended_end = base + timedelta(days=1)

        result = compute_labels(df, window_end, extended_end)
        for col in ("wager", "status", "table_id"):
            self.assertIn(col, result.columns)
        self.assertEqual(result.iloc[0]["wager"], 100.0)
        self.assertEqual(result.iloc[0]["status"], "LOSE")
        self.assertEqual(result.iloc[0]["table_id"], "T1")

    def test_r15_all_null_payout_returns_empty_with_label_columns(self):
        base = datetime(2025, 1, 1)
        df = pd.DataFrame(
            [
                {"canonical_id": "P1", "bet_id": 1, "payout_complete_dtm": None},
                {"canonical_id": "P2", "bet_id": 2, "payout_complete_dtm": None},
            ]
        )
        window_end = base + timedelta(hours=1)
        extended_end = base + timedelta(days=1)

        result = compute_labels(df, window_end, extended_end)
        self.assertEqual(len(result), 0)
        self.assertIn("label", result.columns)
        self.assertIn("censored", result.columns)

    def test_r15_all_censored_rows_have_label_zero(self):
        base = datetime(2025, 1, 1)
        df = pd.DataFrame(
            [
                {"canonical_id": "P1", "bet_id": 1, "payout_complete_dtm": base},
                {"canonical_id": "P2", "bet_id": 2, "payout_complete_dtm": base},
            ]
        )
        window_end = base + timedelta(minutes=1)
        extended_end = base + timedelta(minutes=WALKAWAY_GAP_MIN - 1)

        result = compute_labels(df, window_end, extended_end)
        self.assertTrue(result["censored"].all())
        self.assertTrue((result["label"] == 0).all())


if __name__ == "__main__":
    unittest.main()
