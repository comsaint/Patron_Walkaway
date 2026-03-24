"""Tests for validator datetime handling: naive = HK local (not UTC).

Ensures parse_alerts interprets stored naive datetimes as Hong Kong local time,
and that raw-data convention (__etl_insert_Dtm >= business timestamps) holds
after normalization.
"""

from __future__ import annotations

import sqlite3
import unittest
from zoneinfo import ZoneInfo

import pandas as pd

import trainer.config as config
import trainer.validator as validator_mod

HK_TZ = ZoneInfo(config.HK_TZ)


def _alerts_table_schema_minimal() -> str:
    """Minimal alerts table for parse_alerts (SELECT * FROM alerts)."""
    return """
        CREATE TABLE alerts (
            bet_id TEXT PRIMARY KEY,
            ts TEXT,
            bet_ts TEXT,
            player_id TEXT,
            table_id TEXT,
            position_idx REAL,
            visit_start_ts TEXT,
            visit_end_ts TEXT,
            session_count INTEGER,
            bet_count INTEGER,
            visit_avg_bet REAL,
            historical_avg_bet REAL,
            score REAL,
            session_id TEXT,
            loss_streak INTEGER,
            bets_last_5m REAL,
            bets_last_15m REAL,
            bets_last_30m REAL,
            wager_last_10m REAL,
            wager_last_30m REAL,
            cum_bets REAL,
            cum_wager REAL,
            avg_wager_sofar REAL,
            session_duration_min REAL,
            bets_per_minute REAL
        )
    """


class TestParseAlertsNaiveBetTsInterpretedAsHK(unittest.TestCase):
    """parse_alerts must treat stored naive datetimes as HK local, not UTC.

    Scorer writes bet_ts as tz-naive HK (e.g. 08:37). If validator treated
    that as UTC and converted to HK, it would become 16:37 (+8h bug).
    """

    def test_naive_bet_ts_remains_same_wall_clock_in_hk(self):
        conn = sqlite3.connect(":memory:")
        conn.execute(_alerts_table_schema_minimal())
        # Store bet_ts as naive ISO (how scorer stores it: HK local, no tz)
        conn.execute(
            "INSERT INTO alerts (bet_id, ts, bet_ts, player_id) VALUES (?, ?, ?, ?)",
            ("1", "2026-03-12T16:00:00+08:00", "2026-03-12T08:37:00", 100),
        )
        conn.commit()

        df = validator_mod.parse_alerts(conn)
        self.assertFalse(df.empty, "parse_alerts should return one row")
        self.assertIn("bet_ts", df.columns)

        bet_ts = df["bet_ts"].iloc[0]
        if hasattr(bet_ts, "tzinfo") and bet_ts.tzinfo is not None:
            bet_ts_hk = bet_ts if str(bet_ts.tzinfo) == "Asia/Hong_Kong" else bet_ts.astimezone(HK_TZ)
        else:
            bet_ts_hk = pd.Timestamp(bet_ts).tz_localize(HK_TZ)

        self.assertEqual(bet_ts_hk.hour, 8, "bet_ts should be 08:37 HK, not 16:37 (bug: naive was treated as UTC)")
        self.assertEqual(bet_ts_hk.minute, 37)

    def test_naive_ts_also_interpreted_as_hk(self):
        """ts when stored naive (if ever) should be HK, not UTC."""
        conn = sqlite3.connect(":memory:")
        conn.execute(_alerts_table_schema_minimal())
        conn.execute(
            "INSERT INTO alerts (bet_id, ts, bet_ts, player_id) VALUES (?, ?, ?, ?)",
            ("2", "2026-03-12T10:00:00", "2026-03-12T09:00:00", 101),
        )
        conn.commit()

        df = validator_mod.parse_alerts(conn)
        self.assertFalse(df.empty)
        ts = df["ts"].iloc[0]
        ts_hk = ts.astimezone(HK_TZ) if getattr(ts, "tzinfo", None) else pd.Timestamp(ts).tz_localize(HK_TZ)
        self.assertEqual(ts_hk.hour, 10, "ts stored naive 10:00 should stay 10:00 HK")


class TestRawDatetimeEtlInsertDtmAfterBusinessTimestamps(unittest.TestCase):
    """Raw data invariant: __etl_insert_Dtm >= business timestamps in same record.

    After applying project convention (naive = HK), ETL insert time must be
    later than or equal to payout_complete_dtm / session_end_dtm etc.
    """

    def test_naive_hk_normalization_preserves_etl_after_business(self):
        # Simulate raw rows: business time 08:37, ETL insert 08:40 (same day HK)
        raw = pd.DataFrame({
            "payout_complete_dtm": pd.to_datetime(["2026-03-12 08:37:00", "2026-03-12 09:00:00"]),
            "__etl_insert_Dtm": pd.to_datetime(["2026-03-12 08:40:00", "2026-03-12 09:05:00"]),
        })
        self.assertTrue(raw["payout_complete_dtm"].dt.tz is None)
        self.assertTrue(raw["__etl_insert_Dtm"].dt.tz is None)

        # Project convention: naive = HK local
        pcd = raw["payout_complete_dtm"]
        etl = raw["__etl_insert_Dtm"]
        pcd_hk = pcd.dt.tz_localize(HK_TZ)
        etl_hk = etl.dt.tz_localize(HK_TZ)

        # Invariant: __etl_insert_Dtm >= payout_complete_dtm for each row
        after_or_eq = (etl_hk >= pcd_hk).all()
        self.assertTrue(after_or_eq, "__etl_insert_Dtm must be >= payout_complete_dtm when both interpreted as HK")

    def test_etl_equals_business_is_allowed(self):
        raw = pd.DataFrame({
            "payout_complete_dtm": pd.to_datetime(["2026-03-12 08:37:00"]),
            "__etl_insert_Dtm": pd.to_datetime(["2026-03-12 08:37:00"]),
        })
        pcd_hk = raw["payout_complete_dtm"].dt.tz_localize(HK_TZ)
        etl_hk = raw["__etl_insert_Dtm"].dt.tz_localize(HK_TZ)
        self.assertTrue((etl_hk >= pcd_hk).all())

    def test_etl_before_business_fails_invariant(self):
        """If raw data had etl < business, invariant would fail (sanity check)."""
        raw = pd.DataFrame({
            "payout_complete_dtm": pd.to_datetime(["2026-03-12 08:40:00"]),
            "__etl_insert_Dtm": pd.to_datetime(["2026-03-12 08:37:00"]),
        })
        pcd_hk = raw["payout_complete_dtm"].dt.tz_localize(HK_TZ)
        etl_hk = raw["__etl_insert_Dtm"].dt.tz_localize(HK_TZ)
        self.assertFalse((etl_hk >= pcd_hk).all(), "This row violates invariant; used to verify test logic.")
