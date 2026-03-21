"""Unified Plan v2 T3 — STATUS Code Review risks as MRE / contract tests.

Maps STATUS.md §「Code Review：統一計劃 v2 — T3 validator_metrics + alerts 遷移」.
Tests-only: no production changes.
"""

from __future__ import annotations

import inspect
import re
import sqlite3
import unittest
from pathlib import Path

import pandas as pd

import trainer.serving.validator as validator_mod
from trainer.serving import scorer as scorer_mod

REPO_ROOT = Path(__file__).resolve().parents[2]
VALIDATOR_SRC = REPO_ROOT / "trainer" / "serving" / "validator.py"


def _hk_ts(s: str) -> pd.Timestamp:
    return pd.Timestamp(s, tz="Asia/Hong_Kong")


# ---------------------------------------------------------------------------
# Review #5: _ALERTS_MIGRATION_COLS == scorer._NEW_ALERT_COLS
# ---------------------------------------------------------------------------


class TestT3AlertsMigrationColsMatchScorer(unittest.TestCase):
    """Risk #5: dual maintenance — validator and scorer alert ALTER lists must stay identical."""

    def test_migration_tuples_equal_new_alert_cols(self):
        self.assertEqual(
            validator_mod._ALERTS_MIGRATION_COLS,
            scorer_mod._NEW_ALERT_COLS,
            "validator._ALERTS_MIGRATION_COLS must match scorer._NEW_ALERT_COLS (validator-first DB path)",
        )


# ---------------------------------------------------------------------------
# Review #1: model_version = newest ts in full alerts (not KPI subset) — contract
# ---------------------------------------------------------------------------


class TestT3LatestModelVersionFromAlerts(unittest.TestCase):
    """Risk #1 / #2: _latest_model_version_from_alerts behaviour."""

    def test_newest_ts_row_wins(self):
        df = pd.DataFrame(
            {
                "ts": [_hk_ts("2026-01-01 10:00:00"), _hk_ts("2026-01-02 10:00:00")],
                "model_version": ["v-old", "v-new"],
            }
        )
        self.assertEqual(validator_mod._latest_model_version_from_alerts(df), "v-new")

    def test_mre_global_newest_ts_not_same_as_hypothetical_kpi_only_row(self):
        """Contract: implementation uses full ``alerts`` — stray newer row dominates.

        If product later restricts to KPI subset (Review #1 suggestion), flip this expectation.
        """
        df = pd.DataFrame(
            {
                "ts": [_hk_ts("2026-01-01 12:00:00"), _hk_ts("2026-01-03 12:00:00")],
                "model_version": ["v-kpi-mother", "v-stray-newer"],
            }
        )
        self.assertEqual(
            validator_mod._latest_model_version_from_alerts(df),
            "v-stray-newer",
        )

    def test_nat_ts_row_does_not_win_over_valid_ts_review_2(self):
        df = pd.DataFrame(
            {
                "ts": [pd.NaT, _hk_ts("2026-01-01 12:00:00")],
                "model_version": ["should-not-win", "y"],
            }
        )
        self.assertEqual(validator_mod._latest_model_version_from_alerts(df), "y")

    def test_missing_model_version_column_returns_none(self):
        df = pd.DataFrame({"ts": [_hk_ts("2026-01-01 12:00:00")]})
        self.assertIsNone(validator_mod._latest_model_version_from_alerts(df))

    def test_all_empty_model_versions_returns_none(self):
        df = pd.DataFrame(
            {
                "ts": [_hk_ts("2026-01-01 12:00:00")],
                "model_version": [None],
            }
        )
        self.assertIsNone(validator_mod._latest_model_version_from_alerts(df))


# ---------------------------------------------------------------------------
# Review #7: _append_validator_metrics round-trip
# ---------------------------------------------------------------------------


class TestT3AppendValidatorMetrics(unittest.TestCase):
    """Risk #7: INSERT tolerates total=0 and normal precision."""

    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute(
            """
            CREATE TABLE validator_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recorded_at TEXT NOT NULL,
                model_version TEXT,
                precision REAL NOT NULL,
                total INTEGER NOT NULL,
                matches INTEGER NOT NULL
            )
            """
        )

    def tearDown(self) -> None:
        self.conn.close()

    def test_zero_total_row(self):
        validator_mod._append_validator_metrics(
            self.conn,
            recorded_at="2026-03-21T12:00:00+08:00",
            model_version=None,
            precision=0.0,
            total=0,
            matches=0,
        )
        row = self.conn.execute("SELECT precision, total, matches, model_version FROM validator_metrics").fetchone()
        self.assertEqual(row, (0.0, 0, 0, ""))

    def test_normal_precision_row(self):
        validator_mod._append_validator_metrics(
            self.conn,
            recorded_at="2026-03-21T12:00:00+08:00",
            model_version="v1",
            precision=0.5,
            total=2,
            matches=1,
        )
        row = self.conn.execute(
            "SELECT precision, total, matches, model_version FROM validator_metrics"
        ).fetchone()
        self.assertEqual(row[0], 0.5)
        self.assertEqual(row[1], 2)
        self.assertEqual(row[2], 1)
        self.assertEqual(row[3], "v1")


# ---------------------------------------------------------------------------
# Review #3: validate_once orders metrics before save; save_validation_results commits
# ---------------------------------------------------------------------------


class TestT3ValidateOnceWriteOrderContract(unittest.TestCase):
    """Risk #3: metrics INSERT relies on save_validation_results commit."""

    def test_append_metrics_before_save_validation_results_in_validate_once(self):
        src = VALIDATOR_SRC.read_text(encoding="utf-8")
        m = re.search(r"^def validate_once\(.*?:\n", src, re.MULTILINE)
        self.assertIsNotNone(m, "validate_once not found")
        start = m.start()
        m2 = re.search(r"^def run_validator_loop\(", src[start:], re.MULTILINE)
        self.assertIsNotNone(m2, "run_validator_loop after validate_once not found")
        block = src[start : start + m2.start()]
        i_metrics = block.find("_append_validator_metrics")
        i_save = block.find("save_validation_results")
        self.assertGreater(i_metrics, -1, "_append_validator_metrics missing in validate_once")
        self.assertGreater(i_save, -1, "save_validation_results missing in validate_once")
        self.assertLess(
            i_metrics,
            i_save,
            "Contract: append validator_metrics before save_validation_results",
        )

    def test_save_validation_results_commits(self):
        body = inspect.getsource(validator_mod.save_validation_results)
        self.assertIn("conn.commit()", body)


# ---------------------------------------------------------------------------
# Review #4: no retention for validator_metrics yet — contract (absence)
# ---------------------------------------------------------------------------


class TestT3ValidatorMetricsNoRetentionContract(unittest.TestCase):
    """Risk #4: table grows until optional retention is implemented."""

    def test_prune_validator_retention_does_not_touch_validator_metrics(self):
        body = inspect.getsource(validator_mod.prune_validator_retention)
        self.assertNotIn(
            "validator_metrics",
            body,
            "Contract: no DELETE/prune on validator_metrics until feature lands",
        )


if __name__ == "__main__":
    unittest.main()
