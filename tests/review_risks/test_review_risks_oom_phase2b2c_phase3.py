"""Minimal reproducible tests for Code Review — OOM Phase 2b/2c/3/4 變更 (STATUS.md 2026-03-11).

Reviewer risk points (Code Review section in STATUS.md) are turned into
minimal reproducible tests only. No production code changes.
"""

from __future__ import annotations

import inspect
import logging
import unittest
from datetime import datetime, timedelta

import pandas as pd

# ---------------------------------------------------------------------------
# Imports (match existing test patterns)
# ---------------------------------------------------------------------------
import trainer.identity as identity_mod
import trainer.trainer as trainer_mod

# Labels: use same import pattern as test_labels.py
import importlib
import pathlib
import sys

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
_labels_mod = importlib.import_module("trainer.labels")
compute_labels = _labels_mod.compute_labels

try:
    from trainer.config import ALERT_HORIZON_MIN, LABEL_LOOKAHEAD_MIN, WALKAWAY_GAP_MIN
except Exception:
    LABEL_LOOKAHEAD_MIN = 60
    WALKAWAY_GAP_MIN = 30
    ALERT_HORIZON_MIN = 15

WINDOW_END = datetime(2025, 6, 1)
EXTENDED_END = datetime(2025, 6, 2)


# --- Review #1: compute_labels combined null — dropped count and warning (A16) ---
class TestOomReviewA16ComputeLabelsCombinedNull(unittest.TestCase):
    """Review #1 (low): When rows have null payout and/or null canonical_id, dropped count must match combined_null and a warning must be logged."""

    def test_compute_labels_combined_null_dropped_count_and_warning(self):
        """compute_labels: with mixed null payout / null canonical_id / both, result length = n - combined_null.sum() and one WARNING is logged (A16)."""
        base = datetime(2025, 1, 1)
        # 5 rows: 2 null payout only, 2 null cid only, 1 both null → 5 dropped; 2 valid
        bets_df = pd.DataFrame([
            {"canonical_id": "P1", "bet_id": 1, "payout_complete_dtm": base},
            {"canonical_id": "P2", "bet_id": 2, "payout_complete_dtm": base + timedelta(minutes=1)},
            {"canonical_id": pd.NA, "bet_id": 3, "payout_complete_dtm": base + timedelta(minutes=2)},
            {"canonical_id": pd.NA, "bet_id": 4, "payout_complete_dtm": base + timedelta(minutes=3)},
            {"canonical_id": "P5", "bet_id": 5, "payout_complete_dtm": pd.NaT},
            {"canonical_id": pd.NA, "bet_id": 6, "payout_complete_dtm": pd.NaT},
        ])
        null_payout = bets_df["payout_complete_dtm"].isna()
        null_cid = bets_df["canonical_id"].isna()
        combined_null = null_payout | null_cid
        expected_keep = len(bets_df) - combined_null.sum()

        logger_name = "trainer.labels"
        with self.assertLogs(logger_name, level=logging.WARNING) as cm:
            result = compute_labels(bets_df, WINDOW_END, EXTENDED_END)

        self.assertEqual(len(result), expected_keep, "Dropped row count must equal combined_null.sum() (A16)")
        dropped_logs = [m for m in cm.output if "dropped" in m.lower() and "compute_labels" in m]
        self.assertGreaterEqual(len(dropped_logs), 1, "At least one WARNING about dropped rows (E3/R12) must be logged")


# --- Review #2: build_canonical_mapping_from_links all cleaned to NaN returns empty (A04) ---
class TestOomReviewA04CanonicalFromLinksAllCleanedToNan(unittest.TestCase):
    """Review #2 (low): When all casino_player_id values clean to NaN (e.g. '' or 'null'), build_canonical_mapping_from_links returns empty mapping."""

    def test_build_canonical_mapping_from_links_all_cleaned_to_nan_returns_empty(self):
        """build_canonical_mapping_from_links: casino_player_id '' or 'null' (cleans to NaN) → empty DataFrame with columns [player_id, canonical_id] (A04)."""
        lud = pd.Timestamp("2025-01-01 12:00:00")
        links_df = pd.DataFrame([
            {"player_id": 1, "casino_player_id": "", "lud_dtm": lud},
            {"player_id": 2, "casino_player_id": "null", "lud_dtm": lud},
        ])
        links_df["lud_dtm"] = pd.to_datetime(links_df["lud_dtm"])

        out = identity_mod.build_canonical_mapping_from_links(links_df, set())

        self.assertEqual(len(out), 0, "All casino_player_id clean to NaN → empty mapping (A04)")
        self.assertEqual(list(out.columns), ["player_id", "canonical_id"], "Empty result must have canonical columns")


# --- Review #3: apply_dq sessions single dq_mask + one copy (A10) ---
class TestOomReviewA10ApplyDqSessionsSingleMask(unittest.TestCase):
    """Review #3 (low): apply_dq sessions branch must use single combined mask (FND-02 + FND-04) and one .copy()."""

    def test_apply_dq_sessions_uses_single_dq_mask_and_one_copy(self):
        """apply_dq source: sessions branch must contain dq_mask, FND-02, FND-04 and one sessions[dq_mask].copy() (A10)."""
        src = inspect.getsource(trainer_mod.apply_dq)
        self.assertIn("dq_mask", src, "apply_dq must use single dq_mask for sessions (A10)")
        self.assertIn("FND-02", src, "FND-02 filter must be present")
        self.assertIn("FND-04", src, "FND-04 filter must be present")
        self.assertIn("sessions[dq_mask].copy()", src, "sessions must be filtered by dq_mask once then .copy() (A10)")


if __name__ == "__main__":
    unittest.main()
