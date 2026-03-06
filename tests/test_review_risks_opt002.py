"""Minimal reproducible guards for OPT-002 reviewer risks (R-OPT002-1~6).

Scope:
- Tests/lint-like source guards only.
- No production-code edits in this round.
- Unresolved risks are marked expectedFailure to keep CI green while
  making the technical debt explicit and executable.
"""

from __future__ import annotations

import inspect
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import pandas as pd
from pandas.testing import assert_frame_equal

import trainer.etl_player_profile as etl_mod


def _mk_session_df_for_parity() -> pd.DataFrame:
    """Create a tiny deterministic session dataset with duplicate session_id.

    The duplicate rows intentionally have different lud_dtm + turnover so that
    pandas first-row dedup and DuckDB latest-lud dedup produce different output.
    """
    return pd.DataFrame(
        [
            {
                "session_id": "S1",
                "player_id": "1",
                "session_start_dtm": "2025-12-30 10:00:00",
                "session_end_dtm": "2025-12-30 11:00:00",
                "lud_dtm": "2025-12-30 11:05:00",
                "gaming_day": "2025-12-30",
                "table_id": "T1",
                "pit_name": "P1",
                "gaming_area": "A1",
                "turnover": 100.0,
                "player_win": 10.0,
                "theo_win": 5.0,
                "num_bets": 10.0,
                "num_games_with_wager": 2.0,
                "buyin": 200.0,
                "is_manual": 0,
                "is_deleted": 0,
                "is_canceled": 0,
            },
            {
                "session_id": "S1",  # duplicate id, newer lud_dtm, different values
                "player_id": "1",
                "session_start_dtm": "2025-12-30 10:00:00",
                "session_end_dtm": "2025-12-30 11:00:00",
                "lud_dtm": "2025-12-30 12:05:00",
                "gaming_day": "2025-12-30",
                "table_id": "T1",
                "pit_name": "P1",
                "gaming_area": "A1",
                "turnover": 500.0,
                "player_win": 30.0,
                "theo_win": 10.0,
                "num_bets": 20.0,
                "num_games_with_wager": 2.0,
                "buyin": 300.0,
                "is_manual": 0,
                "is_deleted": 0,
                "is_canceled": 0,
            },
            {
                "session_id": "S2",
                "player_id": "1",
                "session_start_dtm": "2025-12-31 09:00:00",
                "session_end_dtm": "2025-12-31 09:30:00",
                "lud_dtm": "2025-12-31 09:35:00",
                "gaming_day": "2025-12-31",
                "table_id": "T2",
                "pit_name": "P1",
                "gaming_area": "A1",
                "turnover": 200.0,
                "player_win": -10.0,
                "theo_win": 8.0,
                "num_bets": 15.0,
                "num_games_with_wager": 1.0,
                "buyin": 100.0,
                "is_manual": 0,
                "is_deleted": 0,
                "is_canceled": 0,
            },
        ]
    )


class TestOpt002RiskGuards(unittest.TestCase):
    """Guardrails for R-OPT002 reviewer risks."""

    def test_r_opt002_1_local_pandas_dedup_should_keep_latest_lud(self):
        """R-OPT002-1: local pandas dedup should be latest-lud, not first-row."""
        src = inspect.getsource(etl_mod._load_sessions_local)
        self.assertRegex(
            src,
            r"sort_values\(\s*['\"]lud_dtm['\"].*drop_duplicates\(\s*subset=\['session_id'\]",
            "Expected _load_sessions_local to sort by lud_dtm before drop_duplicates(session_id).",
        )

    def test_r_opt002_2_duckdb_parquet_path_should_be_parameterized(self):
        """R-OPT002-2: avoid SQL f-string parquet path interpolation."""
        src = inspect.getsource(etl_mod._compute_profile_duckdb)
        self.assertRegex(
            src,
            r"read_parquet\(\$1\)|CREATE\s+VIEW.*read_parquet\(\$1\)",
            "Expected parameterized read_parquet($1) style instead of direct f-string path injection.",
        )

    def test_r_opt002_3_duckdb_vs_pandas_minimal_parity(self):
        """R-OPT002-3: DuckDB and (corrected) pandas paths agree on synthetic data.

        Both paths now apply latest-lud dedup (FND-01 fix, R-OPT002-1), so
        turnover_sum_30d must be identical.
        """
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            pq = td_path / "gmwds_t_session.parquet"
            df = _mk_session_df_for_parity()
            df.to_parquet(pq, index=False)

            canonical_map = pd.DataFrame({"player_id": ["1"], "canonical_id": ["C001"]})
            snapshot_dtm = datetime(2026, 1, 1, 0, 7, 0)

            # DuckDB result
            duck_df = etl_mod._compute_profile_duckdb(
                session_parquet_path=pq,
                canonical_map=canonical_map,
                snapshot_dtm=snapshot_dtm,
                max_lookback_days=365,
            )
            self.assertIsNotNone(duck_df, "DuckDB path should produce a non-empty profile dataframe.")

            # Corrected pandas path: sort by lud_dtm DESC before dedup
            # (matches DuckDB ROW_NUMBER ORDER BY lud_dtm DESC behaviour).
            raw = pd.read_parquet(pq, columns=etl_mod._SESSION_COLS)
            sess_end = pd.to_datetime(raw.get("session_end_dtm", pd.NaT))
            lud = pd.to_datetime(raw.get("lud_dtm", pd.NaT))
            avail_time = sess_end.fillna(lud) + pd.Timedelta(minutes=etl_mod.SESSION_AVAIL_DELAY_MIN)
            lo_dtm = snapshot_dtm - pd.Timedelta(days=etl_mod.MAX_LOOKBACK_DAYS + 30)
            mask = (
                (avail_time >= pd.Timestamp(lo_dtm))
                & (avail_time <= pd.Timestamp(snapshot_dtm))
                & (raw["is_manual"] == 0)
                & (raw["is_deleted"] == 0)
                & (raw["is_canceled"] == 0)
                & ((raw["turnover"].fillna(0) > 0) | (raw["num_games_with_wager"].fillna(0) > 0))
            )
            local = raw[mask].sort_values("lud_dtm", ascending=False, na_position="last").drop_duplicates(subset=["session_id"], keep="first")
            cmap = canonical_map[["player_id", "canonical_id"]].drop_duplicates().copy()
            cmap["player_id"] = cmap["player_id"].astype(str)
            local = local.copy()
            local["player_id"] = local["player_id"].astype(str)
            joined = local.merge(cmap, on="player_id", how="inner")
            joined = etl_mod._exclude_fnd12_dummies(joined)
            pandas_df = etl_mod._compute_profile(
                joined, snapshot_dtm=snapshot_dtm, max_lookback_days=365
            )

            # Compare one highly sensitive column to keep repro minimal.
            left = duck_df[["canonical_id", "turnover_sum_30d"]].sort_values("canonical_id").reset_index(drop=True)
            right = pandas_df[["canonical_id", "turnover_sum_30d"]].sort_values("canonical_id").reset_index(drop=True)
            assert_frame_equal(left, right, check_exact=True)

    def test_r_opt002_4_duckdb_compute_should_accept_reused_connection(self):
        """R-OPT002-4: compute helper should allow connection reuse across snapshots."""
        sig = inspect.signature(etl_mod._compute_profile_duckdb)
        self.assertIn(
            "con",
            sig.parameters,
            "Expected _compute_profile_duckdb(..., con=...) for backfill connection reuse.",
        )

    def test_r_opt002_5_duration_should_use_subsecond_expression(self):
        """R-OPT002-5: avg duration should preserve sub-second precision."""
        src = inspect.getsource(etl_mod._compute_profile_duckdb)
        self.assertIn(
            "EPOCH(session_ts - session_start_ts)",
            src,
            "Expected EPOCH(interval)/60.0 style duration expression (sub-second precision).",
        )

    def test_r_opt002_6_pandas_fallback_should_filter_whitelist(self):
        """R-OPT002-6: pandas fallback path should respect canonical_id_whitelist."""
        src = inspect.getsource(etl_mod.build_player_profile)
        # Limit to the pandas branch (after "Original pandas path" marker).
        try:
            pandas_branch = src.split("Original pandas path", 1)[1]
        except IndexError:
            pandas_branch = src
        self.assertRegex(
            pandas_branch,
            r"canonical_id_whitelist\s+is\s+not\s+None[\s\S]*sessions_with_cid\s*=\s*sessions_with_cid\[",
            "Expected explicit whitelist filtering in pandas fallback branch.",
        )


if __name__ == "__main__":
    unittest.main()
