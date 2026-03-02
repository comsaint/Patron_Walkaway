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
    repo_root = pathlib.Path(__file__).resolve().parents[1]
    repo_root_str = str(repo_root)
    if repo_root_str not in sys.path:
        sys.path.insert(0, repo_root_str)
    return importlib.import_module("trainer.config")


class TestConfigRequiredConstants(unittest.TestCase):
    """Assert all Phase 1 (SSOT v9) required constants exist and have sane types."""

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

    def test_g1_threshold_gate_exist(self):
        self.assertHasAttr("G1_PRECISION_MIN", (int, float))
        self.assertHasAttr("G1_ALERT_VOLUME_MIN_PER_HOUR", (int, float))
        self.assertHasAttr("G1_FBETA", (int, float))
        self.assertHasAttr("OPTUNA_N_TRIALS", int)

    def test_track_b_constants_exist(self):
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

    def assertHasAttr(self, name: str, expected_type: type | tuple):
        self.assertTrue(hasattr(self.config, name), f"config must define {name}")
        val = getattr(self.config, name)
        if isinstance(expected_type, tuple):
            self.assertIsInstance(val, expected_type, f"{name} must be one of {expected_type}")
        else:
            self.assertIsInstance(val, expected_type, f"{name} must be {expected_type}")


if __name__ == "__main__":
    unittest.main()
