"""Guardrail tests for Code Review — ML API populate casino_player_id (STATUS.md).

Maps each Reviewer risk (§1–§4) to a minimal reproducible test.
Production code is not modified; tests document desired contract.
Some tests may fail until production normalizes empty string / API output type (see STATUS.md).
"""

from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Ensure trainer can be imported (config, etc.)
import trainer.scorer as scorer_mod  # noqa: E402
import trainer.validator as validator_mod  # noqa: E402


# ---------------------------------------------------------------------------
# §1 邊界條件：空字串未正規化為 null
# ---------------------------------------------------------------------------

class TestAppendAlertsCasinoPlayerIdEmptyString(unittest.TestCase):
    """§1: append_alerts — casino_player_id "" or "  " should write NULL to DB (contract)."""

    def _conn_with_alerts_schema(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.executescript(
            """
            CREATE TABLE alerts (
                bet_id TEXT PRIMARY KEY,
                ts TEXT, bet_ts TEXT, player_id TEXT, casino_player_id TEXT, table_id TEXT,
                position_idx REAL, visit_start_ts TEXT, visit_end_ts TEXT,
                session_count INTEGER, bet_count INTEGER, visit_avg_bet REAL, historical_avg_bet REAL,
                score REAL, session_id TEXT, loss_streak INTEGER,
                bets_last_5m REAL, bets_last_15m REAL, bets_last_30m REAL,
                wager_last_10m REAL, wager_last_30m REAL, cum_bets REAL, cum_wager REAL,
                avg_wager_sofar REAL, session_duration_min REAL, bets_per_minute REAL,
                canonical_id TEXT, is_rated_obs INTEGER, reason_codes TEXT, model_version TEXT,
                margin REAL, scored_at TEXT
            );
            """
        )
        return conn

    def test_append_alerts_casino_player_id_empty_string_writes_null(self):
        """Contract: when alerts_df has casino_player_id="", DB column should be NULL.
        Current behavior may write "" until production normalizes (Review §1)."""
        conn = self._conn_with_alerts_schema()
        row = {
            "bet_id": "bid_1",
            "ts": "2026-03-12T10:00:00",
            "bet_ts": "2026-03-12T09:59:00",
            "player_id": "1",
            "casino_player_id": "",
            "table_id": "T1",
            "position_idx": 0.0,
            "visit_start_ts": None,
            "visit_end_ts": None,
            "session_count": 1,
            "bet_count": 1,
            "visit_avg_bet": 100.0,
            "historical_avg_bet": 100.0,
            "score": 0.6,
            "session_id": "s1",
            "loss_streak": 0,
            "bets_last_5m": 1.0,
            "bets_last_15m": 1.0,
            "bets_last_30m": 1.0,
            "wager_last_10m": 100.0,
            "wager_last_30m": 100.0,
            "cum_bets": 1.0,
            "cum_wager": 100.0,
            "avg_wager_sofar": 100.0,
            "session_duration_min": 5.0,
            "bets_per_minute": 0.2,
            "canonical_id": "1",
            "is_rated_obs": 1,
            "reason_codes": "[]",
            "model_version": "v1",
            "margin": 0.1,
            "scored_at": "2026-03-12T10:00:00",
        }
        df = pd.DataFrame([row])
        scorer_mod.append_alerts(conn, df)
        cur = conn.execute("SELECT casino_player_id FROM alerts WHERE bet_id = ?", ("bid_1",))
        val = cur.fetchone()[0]
        self.assertIsNone(val, "casino_player_id='' should be stored as NULL (FND-03 / Review §1)")

    def test_append_alerts_casino_player_id_whitespace_writes_null(self):
        """Contract: when casino_player_id is "  ", DB column should be NULL."""
        conn = self._conn_with_alerts_schema()
        row = {
            "bet_id": "bid_2",
            "ts": "2026-03-12T10:00:00",
            "bet_ts": "2026-03-12T09:59:00",
            "player_id": "2",
            "casino_player_id": "  ",
            "table_id": "T1",
            "position_idx": 0.0,
            "visit_start_ts": None,
            "visit_end_ts": None,
            "session_count": 1,
            "bet_count": 1,
            "visit_avg_bet": 100.0,
            "historical_avg_bet": 100.0,
            "score": 0.6,
            "session_id": "s2",
            "loss_streak": 0,
            "bets_last_5m": 1.0,
            "bets_last_15m": 1.0,
            "bets_last_30m": 1.0,
            "wager_last_10m": 100.0,
            "wager_last_30m": 100.0,
            "cum_bets": 1.0,
            "cum_wager": 100.0,
            "avg_wager_sofar": 100.0,
            "session_duration_min": 5.0,
            "bets_per_minute": 0.2,
            "canonical_id": "2",
            "is_rated_obs": 1,
            "reason_codes": "[]",
            "model_version": "v1",
            "margin": 0.1,
            "scored_at": "2026-03-12T10:00:00",
        }
        df = pd.DataFrame([row])
        scorer_mod.append_alerts(conn, df)
        cur = conn.execute("SELECT casino_player_id FROM alerts WHERE bet_id = ?", ("bid_2",))
        val = cur.fetchone()[0]
        self.assertIsNone(val, "casino_player_id='  ' should be stored as NULL (Review §1)")


class TestValidateAlertRowCasinoPlayerIdEmptyString(unittest.TestCase):
    """§1: validate_alert_row — row['casino_player_id'] == '' should yield res_base['casino_player_id'] is None."""

    def test_validate_alert_row_casino_player_id_empty_string_yields_none(self):
        """Contract: when row has casino_player_id="", res_base['casino_player_id'] should be None."""
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        try:
            from trainer import config
        except ImportError:
            import trainer.config as config
        HK_TZ = ZoneInfo(config.HK_TZ)
        now = datetime.now(HK_TZ)
        # bet_ts must be old enough to pass "too recent" check (47+ min ago)
        past = now - timedelta(hours=2)
        row = pd.Series({
            "ts": past,
            "bet_ts": past,
            "player_id": 1,
            "bet_id": "bid_1",
            "canonical_id": "1",
            "table_id": "T1",
            "session_id": "s1",
            "score": 0.6,
            "model_version": "v1",
            "casino_player_id": "",
        })
        res = validator_mod.validate_alert_row(row, {}, {}, force_finalize=True)
        self.assertIn("casino_player_id", res)
        self.assertIsNone(res["casino_player_id"], "casino_player_id='' should become None (Review §1)")


# ---------------------------------------------------------------------------
# §2 邊界條件：API 層 casino_player_id 型別應為 string | null
# ---------------------------------------------------------------------------

class TestApiAlertsProtocolCasinoPlayerIdType(unittest.TestCase):
    """§2: _alerts_to_protocol_records — casino_player_id in output must be str or None."""

    def setUp(self):
        if "trainer.api_server" not in sys.modules:
            import importlib as _il
            if "config" not in sys.modules:
                sys.modules["config"] = _il.import_module("trainer.config")
            _il.import_module("trainer.api_server")
        self.api = sys.modules["trainer.api_server"]

    def test_alerts_protocol_casino_player_id_float_becomes_str_or_none(self):
        """Contract: when df['casino_player_id'] is 1.0, output should be '1' or None (protocol string|null)."""
        df = pd.DataFrame({
            "ts": ["2026-03-12T10:00:00+08:00"],
            "ts_dt": pd.to_datetime(["2026-03-12 10:00:00"]),
            "bet_id": ["1"],
            "bet_ts": ["2026-03-12 09:59:00"],
            "player_id": ["1"],
            "casino_player_id": [1.0],
            "table_id": ["T1"],
            "position_idx": [0],
            "session_id": ["1"],
            "visit_avg_bet": [100.0],
            "is_rated_obs": [1],
        })
        records = self.api._alerts_to_protocol_records(df)
        self.assertEqual(len(records), 1)
        cid = records[0].get("casino_player_id")
        self.assertTrue(cid is None or isinstance(cid, str), "casino_player_id must be str or None (Review §2)")

    def test_alerts_protocol_casino_player_id_nan_becomes_none(self):
        """Contract: when df['casino_player_id'] is np.nan, output should be None."""
        df = pd.DataFrame({
            "ts": ["2026-03-12T10:00:00+08:00"],
            "ts_dt": pd.to_datetime(["2026-03-12 10:00:00"]),
            "bet_id": ["1"],
            "bet_ts": ["2026-03-12 09:59:00"],
            "player_id": ["1"],
            "casino_player_id": [np.nan],
            "table_id": ["T1"],
            "position_idx": [0],
            "session_id": ["1"],
            "visit_avg_bet": [100.0],
            "is_rated_obs": [1],
        })
        records = self.api._alerts_to_protocol_records(df)
        self.assertEqual(len(records), 1)
        self.assertIsNone(records[0].get("casino_player_id"), "nan should become None (Review §2)")


class TestApiValidationProtocolCasinoPlayerIdType(unittest.TestCase):
    """§2: _validation_to_protocol_records — casino_player_id must be str or None."""

    def setUp(self):
        if "trainer.api_server" not in sys.modules:
            import importlib as _il
            if "config" not in sys.modules:
                sys.modules["config"] = _il.import_module("trainer.config")
            _il.import_module("trainer.api_server")
        self.api = sys.modules["trainer.api_server"]

    def test_validation_protocol_casino_player_id_float_becomes_str_or_none(self):
        """Contract: when df['casino_player_id'] is 1.0, output should be str or None."""
        df = pd.DataFrame({
            "alert_ts": ["2026-03-12T10:00:00"],
            "player_id": [1],
            "bet_id": ["1"],
            "gap_start": [None],
            "result": [0],
            "validated_at": ["2026-03-12T11:00:00"],
            "reason": ["MISS"],
            "bet_ts": ["2026-03-12T09:59:00"],
            "casino_player_id": [1.0],
        })
        records = self.api._validation_to_protocol_records(df)
        self.assertEqual(len(records), 1)
        cid = records[0].get("casino_player_id")
        self.assertTrue(cid is None or isinstance(cid, str), "casino_player_id must be str or None (Review §2)")


# ---------------------------------------------------------------------------
# §3 邊界條件：Validator row 無 casino_player_id 鍵
# ---------------------------------------------------------------------------

class TestValidateAlertRowMissingCasinoPlayerIdKey(unittest.TestCase):
    """§3: validate_alert_row — row without 'casino_player_id' key must not raise; res_base['casino_player_id'] is None."""

    def test_validate_alert_row_missing_casino_player_id_key_no_raise(self):
        """Contract: when row has no 'casino_player_id' key (e.g. old schema), no KeyError; res_base['casino_player_id'] is None."""
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        try:
            from trainer import config
        except ImportError:
            import trainer.config as config
        HK_TZ = ZoneInfo(config.HK_TZ)
        now = datetime.now(HK_TZ)
        past = now - timedelta(hours=2)
        # Minimal row without casino_player_id (simulate legacy alert)
        row = pd.Series({
            "ts": past,
            "bet_ts": past,
            "player_id": 1,
            "bet_id": "bid_1",
            "canonical_id": "1",
            "table_id": "T1",
            "session_id": "s1",
            "score": 0.6,
            "model_version": "v1",
        })
        res = validator_mod.validate_alert_row(row, {}, {}, force_finalize=True)
        self.assertIn("casino_player_id", res)
        self.assertIsNone(res["casino_player_id"], "Missing key must yield None (Review §3)")


# ---------------------------------------------------------------------------
# §4 正確性：save_validation_results 當 final_df 無 casino_player_id 欄
# ---------------------------------------------------------------------------

class TestSaveValidationResultsMissingCasinoPlayerIdColumn(unittest.TestCase):
    """§4: save_validation_results — when final_df has no casino_player_id column (old rows), must not raise; column written as NULL."""

    def _conn_with_validation_results_schema(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.execute(
            """
            CREATE TABLE validation_results (
                bet_id TEXT PRIMARY KEY,
                alert_ts TEXT, validated_at TEXT, player_id TEXT, casino_player_id TEXT, canonical_id TEXT,
                table_id TEXT, position_idx REAL, session_id TEXT, score REAL, result INTEGER,
                gap_start TEXT, gap_minutes REAL, reason TEXT, bet_ts TEXT, model_version TEXT
            )
            """
        )
        return conn

    def test_save_validation_results_missing_casino_player_id_column_no_raise(self):
        """Contract: final_df built without casino_player_id column (simulate old existing_results) must not raise; written as NULL."""
        conn = self._conn_with_validation_results_schema()
        cols = [c for c in validator_mod.VALIDATION_COLUMNS if c != "casino_player_id"]
        row = {c: None for c in cols}
        row["bet_id"] = "test_bet_cpid"
        row["alert_ts"] = "2026-03-12T10:00:00"
        row["validated_at"] = "2026-03-12T11:00:00"
        row["result"] = 0
        final_df = pd.DataFrame([row])
        for c in validator_mod.VALIDATION_COLUMNS:
            if c not in final_df.columns:
                final_df[c] = None
        final_df = final_df[validator_mod.VALIDATION_COLUMNS]
        validator_mod.save_validation_results(conn, final_df)
        cur = conn.execute("SELECT casino_player_id FROM validation_results WHERE bet_id = ?", ("test_bet_cpid",))
        val = cur.fetchone()[0]
        self.assertIsNone(val, "Missing column should write NULL (Review §4)")
