"""tests/test_identity_review_risks_round3.py
=============================================
Guardrail tests for Round 3 identity review risks (R7–R11).

Important:
- These tests are intentionally marked as expected failures until the
  corresponding production fixes are applied.
- They are designed to be minimal, deterministic, and ClickHouse-free.
"""

from __future__ import annotations

import importlib
import inspect
import pathlib
import sys
import unittest
from datetime import datetime, timedelta

import pandas as pd


def _import_identity():
    repo_root = pathlib.Path(__file__).resolve().parents[1]
    repo_root_str = str(repo_root)
    if repo_root_str not in sys.path:
        sys.path.insert(0, repo_root_str)
    return importlib.import_module("trainer.identity")


IDENTITY = _import_identity()


class TestIdentityReviewRisksRound3(unittest.TestCase):
    # -------------------------
    # R7 — FND-03 parity (trim)
    # -------------------------
    def test_r7_clean_casino_player_id_trims_valid_values(self):
        s = pd.Series(["  CARD_A  ", "\tCARD_B\t"])
        result = IDENTITY._clean_casino_player_id(s)
        self.assertEqual(result.iloc[0], "CARD_A")
        self.assertEqual(result.iloc[1], "CARD_B")

    # -----------------------------------------
    # R8 — dummy SQL should be cutoff-consistent
    # -----------------------------------------
    def test_r8_dummy_sql_builder_accepts_cutoff_dtm(self):
        sig = inspect.signature(IDENTITY._build_dummy_sql)
        self.assertIn("cutoff_dtm", sig.parameters)

    def test_r8_dummy_sql_contains_cutoff_filter(self):
        # We only assert the presence of the cutoff predicate; this is a
        # ClickHouse-free way to prevent future leakage in dummy detection.
        dummy_sql = IDENTITY._build_dummy_sql(datetime(2025, 6, 1))
        self.assertIn("COALESCE(session_end_dtm, lud_dtm) <=", dummy_sql)

    # ---------------------------------------------------------
    # R9 — H2 available-time gate uses correct sign (-)
    # ---------------------------------------------------------
    def test_r9_session_lookup_within_delay_window_is_rejected(self):
        # If a session ended only 5 minutes ago, it should be considered
        # not yet available (SESSION_AVAIL_DELAY_MIN is 15).
        obs = datetime(2025, 1, 1, 1, 0, 0)
        session_end = obs - timedelta(minutes=5)
        lookup = lambda sid: {"casino_player_id": "CARD_X", "session_avail_dtm": session_end}
        mapping = pd.DataFrame([(1, "CACHE_CARD")], columns=["player_id", "canonical_id"])
        got = IDENTITY.resolve_canonical_id(1, "S1", mapping, lookup, obs_time=obs)
        self.assertEqual(got, "CACHE_CARD")

    # -------------------------------------------------------
    # R10 — mapping cache lookup supports indexed frames
    # -------------------------------------------------------
    def test_r10_step2_supports_mapping_df_indexed_by_player_id(self):
        mapping = (
            pd.DataFrame([(1, "CACHE_CARD")], columns=["player_id", "canonical_id"])
            .set_index("player_id")
        )
        got = IDENTITY.resolve_canonical_id(1, "S1", mapping, session_lookup=None)
        self.assertEqual(got, "CACHE_CARD")

    # --------------------------------------------
    # R11 — missing required columns raises ValueError
    # --------------------------------------------
    def test_r11_missing_columns_raises_valueerror_with_message(self):
        df = pd.DataFrame({"session_id": ["S1"], "player_id": [1]})
        with self.assertRaises(ValueError) as ctx:
            IDENTITY.build_canonical_mapping_from_df(df, cutoff_dtm=datetime(2025, 6, 1))
        self.assertIn("missing required columns", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

