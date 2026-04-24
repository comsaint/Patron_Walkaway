"""tests/test_config.py
========================
Validates that trainer/config.py defines all required Phase 1 constants (PLAN Step 10).

Uses unittest + import from trainer package; no ClickHouse. Run from repo root:
  python -m unittest tests.test_config -v
"""

from __future__ import annotations

import importlib
import pathlib
import sys
import unittest


def _import_config():
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    repo_root_str = str(repo_root)
    if repo_root_str not in sys.path:
        sys.path.insert(0, repo_root_str)
    return importlib.import_module("trainer.config")


class TestConfigRequiredConstants(unittest.TestCase):
    """Assert all Phase 1 (SSOT v10) required constants exist and have sane types."""

    @classmethod
    def setUpClass(cls):
        cls.config = _import_config()

    def test_business_parameters_exist(self):
        self.assertHasAttr("WALKAWAY_GAP_MIN", int)
        self.assertHasAttr("ALERT_HORIZON_MIN", int)
        self.assertHasAttr("LABEL_LOOKAHEAD_MIN", int)
        self.assertGreater(getattr(self.config, "WALKAWAY_GAP_MIN"), 0)
        self.assertGreater(getattr(self.config, "ALERT_HORIZON_MIN"), 0)

    def test_label_lookahead_equals_x_plus_y(self):
        x = getattr(self.config, "WALKAWAY_GAP_MIN")
        y = getattr(self.config, "ALERT_HORIZON_MIN")
        lookahead = getattr(self.config, "LABEL_LOOKAHEAD_MIN")
        self.assertEqual(lookahead, x + y, "LABEL_LOOKAHEAD_MIN must equal WALKAWAY_GAP_MIN + ALERT_HORIZON_MIN")

    def test_data_availability_delays_exist(self):
        self.assertHasAttr("BET_AVAIL_DELAY_MIN", (int, float))
        self.assertHasAttr("SESSION_AVAIL_DELAY_MIN", (int, float))

    def test_run_boundary_and_gaming_day_exist(self):
        self.assertHasAttr("RUN_BREAK_MIN", (int, float))
        self.assertHasAttr("GAMING_DAY_START_HOUR", (int, float))

    def test_optuna_n_trials_exist(self):
        self.assertHasAttr("OPTUNA_N_TRIALS", int)

    def test_optuna_total_timeout_budget_exists(self):
        self.assertTrue(hasattr(self.config, "OPTUNA_TIMEOUT_SECONDS"))
        v = getattr(self.config, "OPTUNA_TIMEOUT_SECONDS")
        self.assertTrue(v is None or isinstance(v, int))
        if v is not None:
            self.assertGreater(v, 0)

    def test_g1_deprecated_constants_are_numeric_if_present(self):
        """DEC-009/010: G1 constants are deprecated rollback knobs."""
        for name in ("G1_PRECISION_MIN", "G1_ALERT_VOLUME_MIN_PER_HOUR", "G1_FBETA"):
            if hasattr(self.config, name):
                val = getattr(self.config, name)
                self.assertIsInstance(val, (int, float), f"{name} should be numeric if present")

    def test_track_human_constants_exist(self):
        self.assertHasAttr("TABLE_HC_WINDOW_MIN", (int, float))
        self.assertHasAttr("PLACEHOLDER_PLAYER_ID", int)
        self.assertHasAttr("LOSS_STREAK_PUSH_RESETS", bool)
        self.assertHasAttr("HIST_AVG_BET_CAP", (int, float))
        self.assertLess(getattr(self.config, "PLACEHOLDER_PLAYER_ID"), 0)

    def test_sql_and_source_exist(self):
        self.assertHasAttr("CASINO_PLAYER_ID_CLEAN_SQL", str)
        self.assertHasAttr("HK_TZ", str)
        self.assertHasAttr("SOURCE_DB", str)
        self.assertHasAttr("TBET", str)
        self.assertHasAttr("TSESSION", str)

    def test_unrated_volume_log_exists(self):
        """PLAN Step 0: UNRATED_VOLUME_LOG (DEC-021) must exist."""
        self.assertHasAttr("UNRATED_VOLUME_LOG", bool)

    def test_no_nonrated_threshold(self):
        """PLAN Step 0 / DEC-009: v10 single Rated model; no nonrated_threshold constant."""
        for name in dir(self.config):
            self.assertFalse(
                "nonrated_threshold" in name.lower(),
                f"config must NOT define {name} (v10 single Rated model)",
            )

    def test_scorer_poll_defaults_exist_and_positive(self):
        """Scorer defaults in config (PLAN scorer-defaults-in-config); Review: must be positive int."""
        self.assertHasAttr("SCORER_LOOKBACK_HOURS", int)
        self.assertHasAttr("SCORER_LOOKBACK_HOURS_MAX", int)
        self.assertHasAttr("SCORER_POLL_INTERVAL_SECONDS", int)
        self.assertGreater(getattr(self.config, "SCORER_LOOKBACK_HOURS"), 0)
        self.assertGreater(getattr(self.config, "SCORER_LOOKBACK_HOURS_MAX"), 0)
        self.assertLessEqual(
            getattr(self.config, "SCORER_LOOKBACK_HOURS"),
            getattr(self.config, "SCORER_LOOKBACK_HOURS_MAX"),
        )
        self.assertGreater(getattr(self.config, "SCORER_POLL_INTERVAL_SECONDS"), 0)

    def test_scorer_cold_start_window_hours_optional(self):
        """Payout-age cap for scoring: None or positive float; capped at SCORER_LOOKBACK_HOURS_MAX when set."""
        self.assertTrue(hasattr(self.config, "SCORER_COLD_START_WINDOW_HOURS"))
        v = getattr(self.config, "SCORER_COLD_START_WINDOW_HOURS")
        self.assertTrue(v is None or isinstance(v, float))
        if v is not None:
            self.assertGreater(v, 0.0)
            self.assertLessEqual(v, float(getattr(self.config, "SCORER_LOOKBACK_HOURS_MAX")))

    def test_runtime_threshold_max_age_optional(self):
        """T-OnlineCalibration: optional TTL for state DB runtime threshold."""
        self.assertTrue(hasattr(self.config, "RUNTIME_THRESHOLD_MAX_AGE_HOURS"))
        v = getattr(self.config, "RUNTIME_THRESHOLD_MAX_AGE_HOURS")
        self.assertTrue(v is None or isinstance(v, float))

    def assertHasAttr(self, name: str, expected_type: type | tuple):
        self.assertTrue(hasattr(self.config, name), f"config must define {name}")
        val = getattr(self.config, name)
        if isinstance(expected_type, tuple):
            self.assertIsInstance(val, expected_type, f"{name} must be one of {expected_type}")
        else:
            self.assertIsInstance(val, expected_type, f"{name} must be {expected_type}")


if __name__ == "__main__":
    unittest.main()
