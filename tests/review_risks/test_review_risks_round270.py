"""Minimal reproducible tests for Round 75 review risks (R1300-R1304).

Tests-only round: do NOT modify production code here.
Unfixed risks are encoded as expected failures to keep them visible.
"""

from __future__ import annotations

import importlib
import inspect
import pathlib
import sys
import unittest
from datetime import datetime

import pandas as pd


def _import_identity():
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    repo_root_str = str(repo_root)
    if repo_root_str not in sys.path:
        sys.path.insert(0, repo_root_str)
    return importlib.import_module("trainer.identity")


identity_mod = _import_identity()


def _make_sessions(rows):
    defaults = {
        "lud_dtm": pd.Timestamp("2026-03-01 10:00:00"),
        "__etl_insert_Dtm": pd.Timestamp("2026-03-01 10:00:01"),
        "session_end_dtm": pd.Timestamp("2026-03-01 10:30:00"),
        "is_manual": 0,
        "is_deleted": 0,
        "is_canceled": 0,
        "num_games_with_wager": 5,
        "turnover": 100.0,
    }
    out = []
    for r in rows:
        row = dict(defaults)
        row.update(r)
        out.append(row)
    return pd.DataFrame(out)


class TestR1300IdentityDocstringContract(unittest.TestCase):
    """R1300: docstring should list turnover as required input column."""

    def test_build_canonical_mapping_docstring_mentions_turnover(self):
        doc = inspect.getdoc(identity_mod.build_canonical_mapping_from_df) or ""
        self.assertIn(
            "turnover",
            doc,
            "build_canonical_mapping_from_df docstring should explicitly list turnover.",
        )


class TestR1301TurnoverNumericGuard(unittest.TestCase):
    """R1301: object-dtype turnover should not crash identity mapping path."""

    def test_build_canonical_mapping_from_df_string_turnover_no_crash(self):
        df = _make_sessions(
            [
                {
                    "session_id": "S1",
                    "player_id": 1001,
                    "casino_player_id": "CARD_A",
                    "turnover": "50.5",
                },
                {
                    "session_id": "S2",
                    "player_id": 1002,
                    "casino_player_id": "CARD_B",
                    "turnover": "0",
                    "num_games_with_wager": 0,
                },
            ]
        )
        out = identity_mod.build_canonical_mapping_from_df(
            df, cutoff_dtm=datetime(2026, 3, 2, 0, 0, 0)
        )
        self.assertIsInstance(out, pd.DataFrame)


class TestR1302DummyPathTimezoneGuard(unittest.TestCase):
    """R1302: get_dummy_player_ids_from_df should handle mixed tz safely."""

    def test_get_dummy_player_ids_from_df_mixed_tz_no_crash(self):
        df = _make_sessions(
            [
                {
                    "session_id": "S1",
                    "player_id": 2001,
                    "casino_player_id": "CARD_X",
                    "lud_dtm": pd.Timestamp("2026-03-01 10:00:00"),  # tz-naive
                    "session_end_dtm": pd.Timestamp("2026-03-01 10:30:00"),  # tz-naive
                    "num_games_with_wager": 0,
                    "turnover": 0.0,
                }
            ]
        )
        cutoff_aware = pd.Timestamp(
            "2026-03-02 00:00:00", tz="Asia/Hong_Kong"
        ).to_pydatetime()
        out = identity_mod.get_dummy_player_ids_from_df(df, cutoff_dtm=cutoff_aware)
        self.assertIsInstance(out, set)


class TestR1303GhostSessionCoverage(unittest.TestCase):
    """R1303: add explicit guards for ghost-session exclusion semantics."""

    def test_ghost_session_excluded_from_canonical_mapping(self):
        df = _make_sessions(
            [
                {
                    "session_id": "S1",
                    "player_id": 3001,
                    "casino_player_id": "CARD_G1",
                    "turnover": 120.0,
                    "num_games_with_wager": 6,
                },
                {
                    "session_id": "S2",
                    "player_id": 3001,
                    "casino_player_id": "CARD_G1",
                    "turnover": 0.0,
                    "num_games_with_wager": 0,
                },
            ]
        )
        out = identity_mod.build_canonical_mapping_from_df(
            df, cutoff_dtm=datetime(2026, 3, 2, 0, 0, 0)
        )
        self.assertIn(3001, set(out.get("player_id", [])))

    def test_pure_ghost_player_not_in_mapping(self):
        df = _make_sessions(
            [
                {
                    "session_id": "S3",
                    "player_id": 3002,
                    "casino_player_id": "CARD_G2",
                    "turnover": 0.0,
                    "num_games_with_wager": 0,
                }
            ]
        )
        out = identity_mod.build_canonical_mapping_from_df(
            df, cutoff_dtm=datetime(2026, 3, 2, 0, 0, 0)
        )
        self.assertNotIn(3002, set(out.get("player_id", [])))

    def test_ghost_session_excluded_from_dummy_detection(self):
        df = _make_sessions(
            [
                {
                    "session_id": "S4",
                    "player_id": 3003,
                    "casino_player_id": "CARD_G3",
                    "turnover": 200.0,
                    "num_games_with_wager": 5,
                },
                {
                    "session_id": "S5",
                    "player_id": 3003,
                    "casino_player_id": "CARD_G3",
                    "turnover": 0.0,
                    "num_games_with_wager": 0,
                },
            ]
        )
        out = identity_mod.get_dummy_player_ids_from_df(
            df, cutoff_dtm=datetime(2026, 3, 2, 0, 0, 0)
        )
        self.assertNotIn(3003, out)


class TestR1304DecisionLogCoverage(unittest.TestCase):
    """R1304: semantic behavior change should be logged in Decision Log."""

    def test_decision_log_mentions_fnd04_dummy_semantics(self):
        decision_log = (
            pathlib.Path(__file__).resolve().parents[2]
            / ".cursor"
            / "plans"
            / "DECISION_LOG.md"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "ghost sessions 不再計入 session_cnt",
            decision_log,
            "Decision log should record FND-04 impact on FND-12 dummy semantics.",
        )


if __name__ == "__main__":
    unittest.main()
