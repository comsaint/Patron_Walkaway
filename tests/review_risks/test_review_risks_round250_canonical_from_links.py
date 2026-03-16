"""Round 250 Code Review — build_canonical_mapping_from_links 風險點轉成測試。

STATUS.md Round 250 Code Review: convert reviewer risk points to minimal
reproducible tests only. No production code changes.

Reference: PLAN § Canonical mapping 全歷史 Step 3, STATUS Round 250 Review.
"""

from __future__ import annotations

import unittest

import pandas as pd

import trainer.identity as identity_mod

# Reuse test_identity helpers for minimal sessions / links
from tests.unit.test_identity import _make_sessions, build_from_df, T1, T2


def _minimal_links_df(player_id=1, casino_player_id="C1", lud_dtm=None):
    if lud_dtm is None:
        lud_dtm = T1
    return pd.DataFrame([{
        "player_id": player_id,
        "casino_player_id": casino_player_id,
        "lud_dtm": lud_dtm,
    }])


class TestR250_1_DummyPidsNone(unittest.TestCase):
    """Review #1: dummy_pids is None — document current behavior (raises or treat as empty)."""

    def test_build_canonical_mapping_from_links_dummy_pids_none_raises(self):
        """When dummy_pids is None, current implementation raises (TypeError/AttributeError).
        When production adds 'dummy_pids or set()', change this to assert no exception and result equals from_links(links_df, set())."""
        links_df = _minimal_links_df()
        with self.assertRaises((TypeError, AttributeError)):
            identity_mod.build_canonical_mapping_from_links(links_df, None)


class TestR250_2_TypeMismatchDummyExclusion(unittest.TestCase):
    """Review #2: player_id vs dummy_pids type mismatch — dummy may not be excluded."""

    def test_player_id_str_dummy_pids_int_dummy_not_excluded_currently(self):
        """When player_id is str and dummy_pids has int, .isin() does not match; dummy row remains in result.
        Lock current behavior; when production normalizes types, change to assert row is excluded."""
        links_df = pd.DataFrame([{
            "player_id": "1",
            "casino_player_id": "C1",
            "lud_dtm": T1,
        }])
        links_df["lud_dtm"] = pd.to_datetime(links_df["lud_dtm"])
        out = identity_mod.build_canonical_mapping_from_links(links_df, {1})  # int 1
        # Currently str "1" not in {1}, so row is not excluded
        self.assertEqual(len(out), 1, "Type mismatch: str player_id not in {int}; row remains (Review #2)")
        self.assertIn(out.iloc[0]["player_id"], (1, "1"))


class TestR250_6_ParityFromDfAndFromLinks(unittest.TestCase):
    """Review #6 (optional): Same logical input → from_df and from_links produce same mapping."""

    def test_from_links_same_result_as_from_df_for_single_rated_session(self):
        """One rated session: build_canonical_mapping_from_df(sessions, cutoff) matches build_canonical_mapping_from_links(links, set())."""
        sessions = _make_sessions([{
            "session_id": "s1",
            "player_id": 1,
            "casino_player_id": "C1",
            "lud_dtm": T1,
            "session_end_dtm": T1,
        }])
        map_from_df = build_from_df(sessions, cutoff_dtm=T2)
        links = pd.DataFrame([{
            "player_id": 1,
            "casino_player_id": "C1",
            "lud_dtm": T1,
        }])
        links["lud_dtm"] = pd.to_datetime(links["lud_dtm"])
        map_from_links = identity_mod.build_canonical_mapping_from_links(links, set())
        self.assertEqual(len(map_from_df), len(map_from_links))
        if len(map_from_df) > 0:
            self.assertEqual(
                map_from_df.set_index("player_id")["canonical_id"].to_dict(),
                map_from_links.set_index("player_id")["canonical_id"].to_dict(),
                "from_df and from_links must agree on player_id -> canonical_id (Review #6)",
            )


if __name__ == "__main__":
    unittest.main()
