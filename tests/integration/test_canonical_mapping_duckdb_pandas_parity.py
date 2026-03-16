"""PLAN § Canonical mapping 全歷史 + DuckDB 步驟 6 — DuckDB vs pandas parity 測試。

DEC-025: 在同一份小型或抽樣 session 資料上分別執行 DuckDB 路徑與 pandas 路徑，
比對兩者產出之 canonical map（及可選 dummy 集合）一致。

本 parity 測試假設每 session_id 僅一筆列，未涵蓋 FND-01 tiebreaker（__etl_insert_Dtm）
情境；若兩路徑對 tiebreaker 語意不一致，需另加同 session_id 多筆 fixture 驗證。
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import pandas as pd

import trainer.identity as identity_mod
from trainer.trainer import (
    _CANONICAL_MAP_SESSION_COLS,
    build_canonical_links_and_dummy_from_duckdb,
    build_canonical_mapping_from_links,
)


def _make_small_sessions_with_parquet_columns() -> pd.DataFrame:
    """Minimal sessions that satisfy _CANONICAL_MAP_SESSION_COLS and identity _REQUIRED_SESSION_COLS.
    Two rated players (C1, C2) and one dummy (1 session, 1 game) so we get non-trivial mapping + dummies.
    """
    base = {
        "session_id": "",
        "player_id": 0,
        "casino_player_id": "",
        "lud_dtm": pd.Timestamp("2025-01-15 12:00:00"),
        "session_start_dtm": pd.Timestamp("2025-01-15 11:00:00"),
        "session_end_dtm": pd.Timestamp("2025-01-15 12:00:00"),
        "is_manual": 0,
        "is_deleted": 0,
        "is_canceled": 0,
        "num_games_with_wager": 1,
        "turnover": 10.0,
    }
    rows = [
        {**base, "session_id": "s1", "player_id": 10, "casino_player_id": " C1 "},
        {**base, "session_id": "s2", "player_id": 20, "casino_player_id": "C2"},
        # FND-12 dummy: 1 session, 1 game
        {
            **base,
            "session_id": "s3",
            "player_id": 99,
            "casino_player_id": "Dummy",
            "num_games_with_wager": 1,
            "turnover": 0.0,
        },
    ]
    df = pd.DataFrame(rows)
    for col in ("lud_dtm", "session_start_dtm", "session_end_dtm"):
        df[col] = pd.to_datetime(df[col], errors="coerce")
    # session_end_dtm <= train_end for all
    return df


def _normalize_map_for_compare(df: pd.DataFrame) -> pd.DataFrame:
    """Sort and reset index so two mappings can be compared."""
    if df.empty:
        return df
    return df.sort_values(["player_id", "canonical_id"]).reset_index(drop=True)


class TestCanonicalMappingDuckDbPandasParity(unittest.TestCase):
    """PLAN Step 6 / DEC-025: DuckDB path and pandas path produce the same canonical map on small data."""

    def test_duckdb_and_pandas_paths_produce_same_canonical_map_on_small_sessions(self):
        """On a small session Parquet, build_canonical_links_and_dummy_from_duckdb + build_canonical_mapping_from_links
        yields the same canonical map as build_canonical_mapping_from_df (same sessions as DataFrame).
        """
        sessions_df = _make_small_sessions_with_parquet_columns()
        train_end = datetime(2025, 2, 1)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sessions.parquet"
            cols = [c for c in _CANONICAL_MAP_SESSION_COLS if c in sessions_df.columns]
            sessions_df[cols].to_parquet(path, index=False)

            links_df, dummy_pids = build_canonical_links_and_dummy_from_duckdb(path, train_end)
            map_duckdb = build_canonical_mapping_from_links(links_df, dummy_pids)

        # Pandas path: build_canonical_mapping_from_df needs _REQUIRED_SESSION_COLS (no session_start_dtm required)
        map_df = identity_mod.build_canonical_mapping_from_df(sessions_df, cutoff_dtm=train_end)

        a = _normalize_map_for_compare(map_duckdb)
        b = _normalize_map_for_compare(map_df)
        self.assertEqual(
            list(a.columns),
            list(b.columns),
            "Both paths must return DataFrame with [player_id, canonical_id]",
        )
        self.assertEqual(
            len(a),
            len(b),
            "DuckDB path and pandas path must yield same number of mapping rows",
        )
        if not a.empty:
            pd.testing.assert_frame_equal(
                a,
                b,
                check_dtype=False,
                msg="Canonical map from DuckDB path must equal map from pandas path (PLAN Step 6 / DEC-025)",
            )

    def test_duckdb_and_pandas_paths_agree_on_dummy_exclusion(self):
        """Dummy player (FND-12: 1 session, <=1 game) is excluded from mapping in both paths."""
        sessions_df = _make_small_sessions_with_parquet_columns()
        train_end = datetime(2025, 2, 1)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sessions.parquet"
            cols = [c for c in _CANONICAL_MAP_SESSION_COLS if c in sessions_df.columns]
            sessions_df[cols].to_parquet(path, index=False)

            _, dummy_pids = build_canonical_links_and_dummy_from_duckdb(path, train_end)
            self.assertIn(99, dummy_pids, "Player 99 is FND-12 dummy and must be in dummy_pids (DuckDB path)")

        map_df = identity_mod.build_canonical_mapping_from_df(sessions_df, cutoff_dtm=train_end)
        map_player_ids = set(map_df["player_id"].astype(int).tolist()) if not map_df.empty else set()
        self.assertNotIn(
            99,
            map_player_ids,
            "Player 99 (dummy) must not appear in canonical map from pandas path",
        )


if __name__ == "__main__":
    unittest.main()
