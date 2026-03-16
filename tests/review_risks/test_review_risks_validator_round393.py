"""Guardrail tests for Code Review — Round 393 (Validator 對齊舊版).

Maps each Reviewer risk to a minimal reproducible test or lint rule.
Production code is not modified; tests document desired behavior.
Some tests are expectedFailure until production is fixed (see STATUS.md).
"""

from __future__ import annotations

import pathlib
import sqlite3
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

import trainer.validator as validator_mod


_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_VALIDATOR_PATH = _REPO_ROOT / "trainer" / "serving" / "validator.py"
_VALIDATOR_SRC = _VALIDATOR_PATH.read_text(encoding="utf-8")


def _get_validate_once_src() -> str:
    """Return source of validate_once (for lint-style checks)."""
    import ast
    tree = ast.parse(_VALIDATOR_SRC)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "validate_once":
            return ast.get_source_segment(_VALIDATOR_SRC, node) or ""
    raise AssertionError("validate_once not found in validator.py")


class TestValidatorRound393Risk1IsUpgrade(unittest.TestCase):
    """Risk 1: is_upgrade must treat stored result NaN/0/None as upgradable when new is MATCH."""

    def test_is_upgrade_logic_handles_nan_stored_result(self):
        """Rule: is_upgrade must not rely solely on 'not existing_results[key][\"result\"]'
        so that stored result=np.nan (e.g. PENDING) still allows upgrade to MATCH.
        """
        src = _get_validate_once_src()
        # Rule: must have stored_is_match or explicit 1/1.0/True check (fragile: not existing_results[key]["result"])
        self.assertIn("is_upgrade", src)
        # Require that we do not use only the fragile pattern (must have stored_is_match or equivalent)
        self.assertTrue(
            "stored_is_match" in src or ("get(\"result\")" in src and ("1.0" in src or "== 1" in src or "is True" in src)),
            "validator.py: is_upgrade should treat stored result NaN/0 as upgradable when res['result'] is True; "
            "add stored_is_match or explicit check for (True, 1, 1.0).",
        )


class TestValidatorRound393Risk2SessionId(unittest.TestCase):
    """Risk 2: save_validation_results must not raise on non-numeric session_id."""

    def _conn_with_validation_results(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.execute(
            """
            CREATE TABLE validation_results (
                bet_id TEXT PRIMARY KEY,
                alert_ts TEXT,
                validated_at TEXT,
                player_id TEXT,
                casino_player_id TEXT,
                canonical_id TEXT,
                table_id TEXT,
                position_idx REAL,
                session_id TEXT,
                score REAL,
                result INTEGER,
                gap_start TEXT,
                gap_minutes REAL,
                reason TEXT,
                bet_ts TEXT,
                model_version TEXT
            )
            """
        )
        return conn

    def test_save_validation_results_accepts_non_numeric_session_id(self):
        """Rule: save_validation_results(conn, final_df) must not raise when a row has session_id='abc'.
        Currently raises ValueError on int(r.session_id). Desired: safe cast or try/except.
        """
        conn = self._conn_with_validation_results()
        cols = list(validator_mod.VALIDATION_COLUMNS)
        row = {c: None for c in cols}
        row["bet_id"] = "test_bet_1"
        row["alert_ts"] = "2026-01-01T00:00:00"
        row["validated_at"] = "2026-01-01T00:00:00"
        row["session_id"] = "abc"
        row["result"] = 0
        final_df = pd.DataFrame([row])
        for c in cols:
            if c not in final_df.columns:
                final_df[c] = None
        final_df = final_df[cols]
        validator_mod.save_validation_results(conn, final_df)
        cur = conn.execute("SELECT session_id FROM validation_results WHERE bet_id = ?", ("test_bet_1",))
        val = cur.fetchone()[0]
        self.assertIsNotNone(val, "session_id should be stored (or None if product decides to coerce invalid to None)")

    def test_save_validation_results_accepts_float_session_id(self):
        """Sanity: session_id as float (e.g. 12345.0 from DB) should not raise."""
        conn = self._conn_with_validation_results()
        cols = list(validator_mod.VALIDATION_COLUMNS)
        row = {c: None for c in cols}
        row["bet_id"] = "test_bet_2"
        row["alert_ts"] = "2026-01-01T00:00:00"
        row["validated_at"] = "2026-01-01T00:00:00"
        row["session_id"] = 12345.0
        row["result"] = 0
        final_df = pd.DataFrame([row])
        for c in cols:
            if c not in final_df.columns:
                final_df[c] = None
        final_df = final_df[cols]
        validator_mod.save_validation_results(conn, final_df)
        cur = conn.execute("SELECT session_id FROM validation_results WHERE bet_id = ?", ("test_bet_2",))
        self.assertEqual(cur.fetchone()[0], "12345")

    def test_save_validation_results_accepts_nan_session_id(self):
        """session_id=NaN should write NULL/None without raising."""
        conn = self._conn_with_validation_results()
        cols = list(validator_mod.VALIDATION_COLUMNS)
        row = {c: None for c in cols}
        row["bet_id"] = "test_bet_3"
        row["alert_ts"] = "2026-01-01T00:00:00"
        row["validated_at"] = "2026-01-01T00:00:00"
        row["session_id"] = np.nan
        row["result"] = 0
        final_df = pd.DataFrame([row])
        for c in cols:
            if c not in final_df.columns:
                final_df[c] = None
        final_df = final_df[cols]
        validator_mod.save_validation_results(conn, final_df)
        cur = conn.execute("SELECT session_id FROM validation_results WHERE bet_id = ?", ("test_bet_3",))
        self.assertIsNone(cur.fetchone()[0])


class TestValidatorRound393Risk3ExceptionSwallowing(unittest.TestCase):
    """Risk 3: load_existing_results and parse_alerts must not re-raise; return empty on DB error."""

    def test_load_existing_results_returns_empty_dict_when_sql_raises(self):
        """When read_sql_query raises OperationalError, load_existing_results must return {} and not raise."""
        conn = sqlite3.connect(":memory:")
        with patch("pandas.read_sql_query", side_effect=sqlite3.OperationalError("no such table: validation_results")):
            result = validator_mod.load_existing_results(conn)
        self.assertEqual(result, {})

    def test_parse_alerts_returns_empty_dataframe_when_sql_raises(self):
        """When read_sql_query raises in parse_alerts path, must return empty DataFrame and not raise."""
        conn = sqlite3.connect(":memory:")
        with patch("pandas.read_sql_query", side_effect=sqlite3.OperationalError("no such table: alerts")):
            df = validator_mod.parse_alerts(conn)
        self.assertIsInstance(df, pd.DataFrame)
        self.assertTrue(df.empty)


class TestValidatorRound393Risk4VisitLevelRegression(unittest.TestCase):
    """Risk 4: No Visit-level logic regression (Round 393 removed it)."""

    def test_validator_no_visit_level_regression(self):
        """Rule: validator.py must not contain Visit-level dedup identifiers or GAMING_DAY_START_HOUR.
        Prevents accidental reintroduction of _visit_key, visit_matches, etc.
        """
        forbidden = (
            "GAMING_DAY_START_HOUR",
            "_visit_key",
            "visit_matches",
            "visit_total",
            "visit_precision",
        )
        for token in forbidden:
            self.assertNotIn(
                token,
                _VALIDATOR_SRC,
                msg=f"validator.py must not contain {token!r} (Visit-level removed in Round 393).",
            )
