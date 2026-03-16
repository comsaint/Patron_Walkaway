"""Minimal reproducible tests for Round 68 review risks (R1100-R1105).

Tests-only round: we intentionally do not change production code here.
Unfixed risks are encoded as expected failures so they remain visible.
"""

from __future__ import annotations

import inspect
import os
import unittest
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

import trainer.scorer as scorer_mod
import trainer.trainer as trainer_mod


class TestR1100ApplyDqPlayerIdNanGuard(unittest.TestCase):
    """R1100: apply_dq should not let NaN player_id pass E4/F1 guard."""

    def test_apply_dq_drops_nan_player_id(self):
        now = datetime(2026, 3, 1, 12, 0, 0)
        bets = pd.DataFrame(
            {
                "bet_id": [1, 2],
                "session_id": [11, 12],
                "player_id": [np.nan, 1001],
                "table_id": [1, 1],
                "payout_complete_dtm": [now - timedelta(minutes=10), now - timedelta(minutes=5)],
                "wager": [100, 200],
                "status": ["LOSE", "WIN"],
            }
        )
        sessions = pd.DataFrame(
            {
                "session_id": [11, 12],
                "player_id": [1001, 1001],
                "session_start_dtm": [now - timedelta(hours=1), now - timedelta(hours=1)],
                "session_end_dtm": [now - timedelta(minutes=20), now - timedelta(minutes=15)],
                "lud_dtm": [now - timedelta(minutes=20), now - timedelta(minutes=15)],
                "is_manual": [0, 0],
                "is_deleted": [0, 0],
                "is_canceled": [0, 0],
                "turnover": [1000, 1200],
                "num_games_with_wager": [5, 6],
            }
        )

        bets_clean, _ = trainer_mod.apply_dq(
            bets=bets,
            sessions=sessions,
            window_start=now - timedelta(hours=2),
            extended_end=now,
            bets_history_start=now - timedelta(hours=3),
        )

        self.assertTrue(
            bets_clean["player_id"].notna().all(),
            "apply_dq should drop NaN player_id rows as defense-in-depth for E4/F1.",
        )


class TestR1101ParquetFilterEfficiencyGuard(unittest.TestCase):
    """R1101: local parquet quick-filters should be merged to reduce copy overhead."""

    def test_load_local_parquet_should_use_single_mask_filter(self):
        src = inspect.getsource(trainer_mod.load_local_parquet)
        self.assertIn(
            "_mask =",
            src,
            "load_local_parquet should use one combined boolean mask to avoid double copy.",
        )


class TestR1102ScorerBetsIsNotNullGuard(unittest.TestCase):
    """R1102: scorer bets query should explicitly require player_id IS NOT NULL."""

    def test_scorer_bets_query_contains_player_id_is_not_null(self):
        src = inspect.getsource(scorer_mod.fetch_recent_data)
        self.assertIn(
            "player_id IS NOT NULL",
            src,
            "scorer fetch_recent_data bets query should include explicit player_id IS NOT NULL guard.",
        )


class TestR1103ScorerSessionFnd04Guard(unittest.TestCase):
    """R1103: scorer session query should include FND-04 turnover activity guard."""

    def test_scorer_session_query_contains_fnd04_turnover_guard(self):
        src = inspect.getsource(scorer_mod.fetch_recent_data)
        self.assertTrue(
            "COALESCE(turnover, 0) > 0 OR COALESCE(num_games_with_wager, 0) > 0" in src
            and "COALESCE(turnover, 0) AS turnover" in src,
            "scorer session query should include turnover column + FND-04 activity filter.",
        )


class TestR1104UnratedVolumeLogUsage(unittest.TestCase):
    """R1104: scorer should consume UNRATED_VOLUME_LOG config flag."""

    def test_scorer_should_reference_unrated_volume_log_flag(self):
        src = inspect.getsource(scorer_mod)
        self.assertIn(
            "UNRATED_VOLUME_LOG",
            src,
            "scorer should consume UNRATED_VOLUME_LOG when emitting unrated volume telemetry.",
        )


class TestR1105SchemaNullabilityCheck(unittest.TestCase):
    """R1105: add an integration probe for ClickHouse player_id nullability."""

    @unittest.skipUnless(
        os.getenv("RUN_CH_SCHEMA_TESTS") == "1",
        "Set RUN_CH_SCHEMA_TESTS=1 and provide ClickHouse access to run schema probe.",
    )
    def test_clickhouse_t_bet_player_id_schema_probe(self):
        client = trainer_mod.get_clickhouse_client()
        df = client.query_df(
            """
            SELECT type
            FROM system.columns
            WHERE database = %(db)s AND table = %(table)s AND name = 'player_id'
            LIMIT 1
            """,
            parameters={"db": trainer_mod.SOURCE_DB, "table": trainer_mod.TBET},
        )
        self.assertGreaterEqual(len(df), 1, "system.columns should return player_id type.")


if __name__ == "__main__":
    unittest.main()
