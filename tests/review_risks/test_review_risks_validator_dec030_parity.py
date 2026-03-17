"""Guardrail tests for Code Review — Validator DEC-030 parity (STATUS.md).

Maps each Reviewer risk (§1–§3) to a minimal reproducible test.
Production code is not modified; tests document desired behavior.

Known state:
- §1 test_gap_start_before_alert_returns_false: FAILS until find_gap_within_window
  enforces gap_start >= alert_ts (Code Review §1 fix). Do not mark expectedFailure;
  use as acceptance test when applying the fix.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

# Use serving implementation to access find_gap_within_window and config
from trainer.serving import validator as validator_impl

try:
    import config as _config
except Exception:
    import trainer.config as _config

HK_TZ = ZoneInfo(_config.HK_TZ)


# ---------------------------------------------------------------------------
# §1 find_gap_within_window: gap start must be >= alert_ts (labels parity)
# ---------------------------------------------------------------------------

class TestFindGapWithinWindowGapStartNotBeforeAlert(unittest.TestCase):
    """§1: Gap start before alert_ts must not yield MATCH (trainer/labels.py parity)."""

    def test_gap_start_before_alert_returns_false(self):
        """When base_start (gap start) is before alert_ts and gap length >= WALKAWAY_GAP_MIN,
        find_gap_within_window must return (False, None, 0.0) so validator does not MATCH
        where trainer would assign label=0 (gap_start not in [bet_ts, bet_ts+ALERT_HORIZON_MIN]).
        """
        alert_ts = datetime(2026, 3, 15, 10, 0, 0, tzinfo=HK_TZ)
        base_start = alert_ts - timedelta(minutes=14)  # gap start 14 min before alert
        # One bet at alert_ts+16min → gap from base_start to that bet = 30 min
        bet_times = [alert_ts + timedelta(minutes=16)]

        is_true, gap_start, gap_minutes = validator_impl.find_gap_within_window(
            alert_ts, bet_times, base_start=base_start
        )

        self.assertFalse(is_true, "gap start before alert_ts must not yield MATCH (labels parity)")
        self.assertIsNone(gap_start)
        self.assertEqual(gap_minutes, 0.0)


# ---------------------------------------------------------------------------
# §2 Validator config must be trainer config (constants parity)
# ---------------------------------------------------------------------------

class TestValidatorConfigSourceContract(unittest.TestCase):
    """§2: Validator module must use config with trainer constants (WALKAWAY_GAP_MIN, LABEL_LOOKAHEAD_MIN)."""

    def test_validator_config_has_trainer_constants(self):
        """Contract: the config used by validator.serving must expose trainer SSOT constants."""
        config = getattr(validator_impl, "config", None)
        self.assertIsNotNone(config, "validator module must have 'config' attribute")
        self.assertEqual(
            getattr(config, "WALKAWAY_GAP_MIN", None),
            30,
            "validator config.WALKAWAY_GAP_MIN must be 30 (trainer SSOT)",
        )
        self.assertEqual(
            getattr(config, "LABEL_LOOKAHEAD_MIN", None),
            45,
            "validator config.LABEL_LOOKAHEAD_MIN must be 45 (trainer SSOT)",
        )

    def test_validator_config_source_is_trainer(self):
        """Contract: config module name should indicate trainer (avoid cwd config shadowing)."""
        config = getattr(validator_impl, "config", None)
        self.assertIsNotNone(config)
        self.assertIn(
            "trainer",
            getattr(config, "__name__", ""),
            "validator must use config from trainer (not cwd config.py)",
        )


# ---------------------------------------------------------------------------
# §3 bet_cache and row bet_ts tz consistency (boundary)
# ---------------------------------------------------------------------------

class TestValidateAlertRowTzConsistency(unittest.TestCase):
    """§3: Document behavior when bet_cache and row bet_ts tz differ (naive vs aware)."""

    def test_consistent_tz_aware_no_type_error(self):
        """When bet_ts (row) and bet_cache datetimes are both tz-aware HK, no TypeError."""
        now = datetime.now(HK_TZ)
        past = now - timedelta(hours=2)
        row = pd.Series({
            "ts": past,
            "bet_ts": past,
            "player_id": 1,
            "bet_id": "b1",
            "canonical_id": "1",
            "table_id": "T1",
            "session_id": 1,
            "score": 0.5,
            "model_version": "v1",
        })
        # bet_list same tz as bet_ts (aware HK)
        bet_cache = {"1": [past, past + timedelta(minutes=50)]}

        try:
            res = validator_impl.validate_alert_row(row, bet_cache, {}, force_finalize=True)
        except TypeError as e:
            self.fail(f"tz-consistent (aware HK) must not raise TypeError: {e}")
        self.assertIn("result", res)

    def test_naive_bet_cache_with_aware_bet_ts_raises_type_error(self):
        """When bet_cache has naive datetimes and row has tz-aware bet_ts, current code raises TypeError.
        Test documents current behavior; if production adds tz normalization, this test may need updating.
        """
        now = datetime.now(HK_TZ)
        past_aware = now - timedelta(hours=2)
        past_naive = past_aware.replace(tzinfo=None)
        row = pd.Series({
            "ts": past_aware,
            "bet_ts": past_aware,
            "player_id": 1,
            "bet_id": "b1",
            "canonical_id": "1",
            "table_id": "T1",
            "session_id": 1,
            "score": 0.5,
            "model_version": "v1",
        })
        bet_cache = {"1": [past_naive, past_naive + timedelta(minutes=50)]}

        with self.assertRaises(TypeError):
            validator_impl.validate_alert_row(row, bet_cache, {}, force_finalize=True)
