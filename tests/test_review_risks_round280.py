"""Minimal reproducible tests for OOM/DECIMAL review risks (Round 280).

Tests-only change set:
- No production code edits.
- Known unresolved risks are encoded as expected failures so they stay visible
  without breaking CI.
"""

from __future__ import annotations

import inspect
import warnings
import unittest

import pandas as pd

import trainer.duckdb_schema as duckdb_schema_mod
import trainer.trainer as trainer_mod


class TestR280ApplyDqRegressionGuards(unittest.TestCase):
    """Guards for apply_dq behavior after OOM refactor."""

    def test_apply_dq_to_numeric_happens_before_combined_mask(self):
        """Risk R-OOM-5: coercion must run before _dq_mask construction."""
        src = inspect.getsource(trainer_mod.apply_dq)
        to_numeric_pos = src.find('pd.to_numeric(bets.get(col), errors="coerce")')
        mask_pos = src.find("_dq_mask = (")
        self.assertNotEqual(to_numeric_pos, -1, "Missing to_numeric coercion in apply_dq")
        self.assertNotEqual(mask_pos, -1, "Missing combined _dq_mask in apply_dq")
        self.assertLess(
            to_numeric_pos,
            mask_pos,
            "to_numeric must happen before _dq_mask so coerced NaN rows are dropped",
        )

    def test_apply_dq_no_settingwithcopywarning_on_minimal_input(self):
        """Risk R-OOM-6: refactor should not trigger SettingWithCopyWarning."""
        bets = pd.DataFrame(
            {
                "bet_id": [1, "2", "bad-id"],
                "session_id": [10, 11, 12],
                "player_id": [100, 101, 102],
                "table_id": [1, 1, 1],
                "payout_complete_dtm": pd.to_datetime(
                    ["2025-03-01 10:00:00", "2025-03-01 10:01:00", "2025-03-01 10:02:00"]
                ),
                "wager": [100.0, 0.0, 200.0],
                "status": ["LOSE", "WIN", "PUSH"],
            }
        )
        sessions = pd.DataFrame(
            {
                "session_id": [10, 11, 12],
                "player_id": [100, 101, 102],
                "session_start_dtm": pd.to_datetime(
                    ["2025-03-01 09:00:00", "2025-03-01 09:00:00", "2025-03-01 09:00:00"]
                ),
                "is_manual": [0, 0, 0],
                "is_deleted": [0, 0, 0],
                "is_canceled": [0, 0, 0],
                "turnover": [100.0, 100.0, 100.0],
                "num_games_with_wager": [1, 1, 1],
            }
        )
        ws = pd.Timestamp("2025-03-01 00:00:00").to_pydatetime()
        ee = pd.Timestamp("2025-03-02 00:00:00").to_pydatetime()

        with warnings.catch_warnings():
            warnings.simplefilter("error", pd.errors.SettingWithCopyWarning)
            bets_out, sessions_out = trainer_mod.apply_dq(bets, sessions, ws, ee)

        self.assertIsInstance(bets_out, pd.DataFrame)
        self.assertIsInstance(sessions_out, pd.DataFrame)


class TestR280ResolvedRisks(unittest.TestCase):
    """Risks from Round-280 review — now resolved in production code."""

    def test_add_track_b_features_should_preserve_pure_function_contract(self):
        """Risk R-OOM-1 resolved: copy taken inside function, not mutating caller."""
        src = inspect.getsource(trainer_mod.add_track_b_features)
        self.assertIn(
            "df = bets.copy()",
            src,
            "Preferred contract: avoid mutating caller-owned DataFrame in-place",
        )

    def test_required_bet_cols_should_not_include_session_only_fields(self):
        """Risk R-OOM-2 resolved: session-only fields removed from pushdown list."""
        req = set(trainer_mod._REQUIRED_BET_PARQUET_COLS)  # pylint: disable=protected-access
        self.assertNotIn("lud_dtm", req)
        self.assertNotIn("__etl_insert_Dtm", req)

    def test_required_bet_cols_should_stay_in_sync_with_clickhouse_select(self):
        """Risk R-OOM-3 resolved: all pushdown cols are present in ClickHouse SELECT."""
        ch_src = trainer_mod._BET_SELECT_COLS  # pylint: disable=protected-access
        for col in trainer_mod._REQUIRED_BET_PARQUET_COLS:  # pylint: disable=protected-access
            self.assertIn(
                col,
                ch_src,
                f"{col} exists in Parquet pushdown list but not in ClickHouse SELECT list",
            )

    def test_prepare_bets_for_duckdb_should_avoid_extra_full_copy(self):
        """Risk R-OOM-4 resolved: helper now mutates in-place, no extra copy."""
        src = inspect.getsource(duckdb_schema_mod.prepare_bets_for_duckdb)
        self.assertNotIn(
            "out = bets_df.copy()",
            src,
            "Current implementation duplicates DataFrame before dtype casts",
        )

    def test_prepare_bets_decimal_detection_should_be_backend_agnostic(self):
        """Risk R-DECIMAL-1 resolved: uses 'decimal' in str(dtype).lower()."""
        src = inspect.getsource(duckdb_schema_mod.prepare_bets_for_duckdb)
        self.assertIn(
            '"decimal" in',
            src,
            "Prefer robust check: 'decimal' in str(dtype).lower()",
        )


if __name__ == "__main__":
    unittest.main()
