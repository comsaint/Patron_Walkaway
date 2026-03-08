"""Unit tests for trainer.schema_io (Post-Load Normalizer). PLAN.md § Post-Load Normalizer Phase 1."""

from __future__ import annotations

import pathlib
import sys
import unittest

import pandas as pd
import numpy as np

# repo root on path for trainer imports
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from trainer.schema_io import (  # noqa: E402
    BET_CATEGORICAL_COLUMNS,
    SESSION_CATEGORICAL_COLUMNS,
    BET_KEY_NUMERIC_COLUMNS,
    SESSION_KEY_NUMERIC_COLUMNS,
    normalize_bets_sessions,
)


class TestSchemaIOConstants(unittest.TestCase):
    def test_bet_categorical_defined(self):
        self.assertIn("table_id", BET_CATEGORICAL_COLUMNS)
        self.assertIn("position_idx", BET_CATEGORICAL_COLUMNS)
        self.assertIn("is_back_bet", BET_CATEGORICAL_COLUMNS)

    def test_session_categorical_defined(self):
        self.assertIn("table_id", SESSION_CATEGORICAL_COLUMNS)

    def test_bet_key_numeric_defined(self):
        self.assertIn("bet_id", BET_KEY_NUMERIC_COLUMNS)
        self.assertIn("session_id", BET_KEY_NUMERIC_COLUMNS)
        self.assertIn("player_id", BET_KEY_NUMERIC_COLUMNS)

    def test_session_key_numeric_defined(self):
        self.assertIn("session_id", SESSION_KEY_NUMERIC_COLUMNS)
        self.assertIn("player_id", SESSION_KEY_NUMERIC_COLUMNS)


class TestNormalizeBetsSessions(unittest.TestCase):
    def test_returns_copies_does_not_mutate_input(self):
        bets = pd.DataFrame({"bet_id": [1], "session_id": [10], "table_id": [1005]})
        sessions = pd.DataFrame({"session_id": [10], "player_id": [100], "table_id": [1005]})
        b_orig = bets.copy()
        s_orig = sessions.copy()
        b_out, s_out = normalize_bets_sessions(bets, sessions)
        pd.testing.assert_frame_equal(bets, b_orig)
        pd.testing.assert_frame_equal(sessions, s_orig)
        self.assertIsNot(b_out, bets)
        self.assertIsNot(s_out, sessions)

    def test_categorical_columns_become_category_dtype(self):
        bets = pd.DataFrame({
            "bet_id": [1],
            "table_id": [1005],
            "position_idx": [0],
            "is_back_bet": [1],
        })
        sessions = pd.DataFrame({"session_id": [10], "table_id": [1005]})
        b_out, s_out = normalize_bets_sessions(bets, sessions)
        self.assertEqual(b_out["table_id"].dtype.name, "category")
        self.assertEqual(b_out["position_idx"].dtype.name, "category")
        self.assertEqual(b_out["is_back_bet"].dtype.name, "category")
        self.assertEqual(s_out["table_id"].dtype.name, "category")

    def test_categorical_preserves_nan(self):
        bets = pd.DataFrame({
            "bet_id": [1, 2],
            "table_id": [1005, np.nan],
            "position_idx": [0, 1],
        })
        b_out, _ = normalize_bets_sessions(bets, pd.DataFrame())
        self.assertEqual(b_out["table_id"].dtype.name, "category")
        self.assertTrue(pd.isna(b_out["table_id"].iloc[1]), "NaN must be preserved in category")

    def test_key_numeric_coerced_no_fillna(self):
        bets = pd.DataFrame({
            "bet_id": [1, 2],
            "session_id": [10, 20],
            "player_id": [100, np.nan],
        })
        sessions = pd.DataFrame({"session_id": [10], "player_id": [100]})
        b_out, s_out = normalize_bets_sessions(bets, sessions)
        self.assertTrue(pd.api.types.is_numeric_dtype(b_out["bet_id"]))
        self.assertTrue(pd.api.types.is_numeric_dtype(b_out["session_id"]))
        self.assertTrue(pd.api.types.is_numeric_dtype(b_out["player_id"]))
        self.assertTrue(pd.isna(b_out["player_id"].iloc[1]))

    def test_only_existing_columns_touched(self):
        bets = pd.DataFrame({"bet_id": [1], "wager": [10.0]})  # no table_id, session_id
        sessions = pd.DataFrame({"session_id": [10]})  # no table_id, no player_id
        b_out, s_out = normalize_bets_sessions(bets, sessions)
        self.assertIn("bet_id", b_out.columns)
        self.assertIn("wager", b_out.columns)
        self.assertTrue(pd.api.types.is_numeric_dtype(b_out["bet_id"]))
        self.assertTrue(pd.api.types.is_numeric_dtype(s_out["session_id"]))

    def test_empty_bets_empty_sessions(self):
        b_out, s_out = normalize_bets_sessions(pd.DataFrame(), pd.DataFrame())
        self.assertTrue(b_out.empty)
        self.assertTrue(s_out.empty)

    def test_etl_style_empty_bets_sessions_with_data(self):
        sessions_raw = pd.DataFrame({
            "session_id": [10, 20],
            "player_id": [100, 200],
            "table_id": [1005, 1006],
        })
        b_out, s_out = normalize_bets_sessions(pd.DataFrame(), sessions_raw)
        self.assertTrue(b_out.empty)
        self.assertEqual(len(s_out), 2)
        self.assertEqual(s_out["table_id"].dtype.name, "category")
        self.assertTrue(pd.api.types.is_numeric_dtype(s_out["session_id"]))

    def test_string_key_numeric_coerced(self):
        bets = pd.DataFrame({"bet_id": ["1", "2"], "session_id": [10, 20]})
        b_out, _ = normalize_bets_sessions(bets, pd.DataFrame())
        self.assertTrue(pd.api.types.is_numeric_dtype(b_out["bet_id"]))
        np.testing.assert_array_equal(b_out["bet_id"].values, [1, 2])


class TestRound150ReviewRisks(unittest.TestCase):
    """Minimal reproducible tests for Round 150 Review risk points. Tests only; no production changes."""

    def test_key_numeric_dtype_float64_when_nan_present(self):
        """R150-1: Key numeric column with NaN becomes float64 (or numeric holding NaN)."""
        bets = pd.DataFrame({
            "bet_id": [1, 2],           # all int
            "session_id": [10, 20],
            "player_id": [100, np.nan],  # has NaN
        })
        b_out, _ = normalize_bets_sessions(bets, pd.DataFrame())
        self.assertTrue(pd.api.types.is_numeric_dtype(b_out["bet_id"]))
        self.assertTrue(pd.api.types.is_numeric_dtype(b_out["player_id"]))
        self.assertTrue(pd.isna(b_out["player_id"].iloc[1]))
        self.assertEqual(b_out["player_id"].dtype.name, "float64")

    def test_rejects_none_bets_or_sessions(self):
        """R150-2: Passing None raises (currently AttributeError; review recommends TypeError)."""
        with self.assertRaises((TypeError, AttributeError)):
            normalize_bets_sessions(None, pd.DataFrame())
        with self.assertRaises((TypeError, AttributeError)):
            normalize_bets_sessions(pd.DataFrame(), None)

    def test_categorical_mixed_type_creates_multiple_categories(self):
        """R150-3: Mixed-type categorical locks current behaviour: three distinct categories."""
        bets = pd.DataFrame({
            "bet_id": [1, 2, 3],
            "table_id": [1005, "1005", 1006],
        })
        b_out, _ = normalize_bets_sessions(bets, pd.DataFrame())
        self.assertEqual(b_out["table_id"].dtype.name, "category")
        self.assertEqual(len(b_out["table_id"].cat.categories), 3)

    def test_untouched_columns_unchanged(self):
        """R150-4: Columns not in normalizer constants (wager, payout_complete_dtm) unchanged."""
        bets = pd.DataFrame({
            "bet_id": [1],
            "table_id": [1005],
            "wager": [10.0],
            "payout_complete_dtm": pd.to_datetime(["2025-01-01 12:00:00"]),
        })
        sessions = pd.DataFrame({"session_id": [10], "player_id": [100]})
        b_out, s_out = normalize_bets_sessions(bets, sessions)
        self.assertEqual(b_out["wager"].dtype, bets["wager"].dtype)
        self.assertEqual(b_out["payout_complete_dtm"].dtype, bets["payout_complete_dtm"].dtype)
        pd.testing.assert_series_equal(b_out["wager"], bets["wager"], check_dtype=True)
        pd.testing.assert_series_equal(
            b_out["payout_complete_dtm"], bets["payout_complete_dtm"], check_dtype=True
        )

    def test_duplicate_columns_raise(self):
        """R150-5: Duplicate column names: current behaviour is to raise (pd.to_numeric gets DataFrame)."""
        bets = pd.DataFrame([[1, 2]], columns=["bet_id", "bet_id"])
        with self.assertRaises(TypeError):
            normalize_bets_sessions(bets, pd.DataFrame())

    def test_empty_dataframe_with_columns(self):
        """R150-6: Empty DataFrame with columns: dtypes still applied, zero rows."""
        bets = pd.DataFrame(columns=["bet_id", "session_id", "table_id"])
        sessions = pd.DataFrame(columns=["session_id", "table_id"])
        b_out, s_out = normalize_bets_sessions(bets, sessions)
        self.assertEqual(len(b_out), 0)
        self.assertEqual(len(s_out), 0)
        self.assertTrue(pd.api.types.is_numeric_dtype(b_out["bet_id"]))
        self.assertEqual(b_out["table_id"].dtype.name, "category")
        self.assertTrue(pd.api.types.is_numeric_dtype(s_out["session_id"]))
        self.assertEqual(s_out["table_id"].dtype.name, "category")


class TestRound165ReviewRisks(unittest.TestCase):
    """Minimal reproducible tests for Round 165 Review (README Data loading & preprocessing). Tests only; no production changes."""

    def test_readme_data_loading_section_lists_all_five_normalizer_entries(self):
        """R165-1: README 'Data loading & preprocessing' must document all five normalizer entry points (doc/code drift risk)."""
        readme_path = _REPO_ROOT / "README.md"
        self.assertTrue(readme_path.exists(), "README.md at repo root must exist")
        text = readme_path.read_text(encoding="utf-8")
        # Extract section between "### Data loading & preprocessing" and next "### "
        marker = "### Data loading & preprocessing"
        idx = text.find(marker)
        self.assertGreaterEqual(idx, 0, "README must contain 'Data loading & preprocessing' section")
        rest = text[idx + len(marker) :]
        end = rest.find("\n### ")
        section = rest if end < 0 else rest[:end]
        required_keywords = [
            "process_chunk",
            "sessions-only",
            "backtester",
            "score_once",
            "etl_player_profile",
        ]
        for kw in required_keywords:
            self.assertIn(kw, section, f"README Data loading section must mention '{kw}' (Round 165 Review §1)")

    def test_etl_call_contract_empty_bets_returns_copy_and_normalized_sessions(self):
        """R165-2: ETL call contract — normalize_bets_sessions(pd.DataFrame(), sessions) returns copy and normalized dtypes."""
        sessions_df = pd.DataFrame({
            "session_id": [10, 20],
            "player_id": [100, 200],
            "table_id": [1005, 1006],
        })
        empty_bets = pd.DataFrame()
        b_out, s_out = normalize_bets_sessions(empty_bets, sessions_df)
        self.assertIsNot(s_out, sessions_df, "ETL path must receive sessions as copy, not mutate input")
        self.assertIsNot(b_out, sessions_df)
        self.assertTrue(b_out.empty, "Empty bets in must yield empty bets out")
        self.assertEqual(len(s_out), 2)
        self.assertEqual(s_out["table_id"].dtype.name, "category", "session table_id must be categorical")
        self.assertTrue(pd.api.types.is_numeric_dtype(s_out["session_id"]), "session_id must be numeric")
        self.assertTrue(pd.api.types.is_numeric_dtype(s_out["player_id"]), "player_id must be numeric")
