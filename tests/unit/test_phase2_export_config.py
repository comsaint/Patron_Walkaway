"""
Phase 2 T5/T6 Code Review §1: Config PREDICTION_EXPORT_* and PREDICTION_LOG_RETENTION_* minimal tests.

- Defaults are int and in reasonable range (no invalid env).
- Subprocess: invalid env causes import/read to fail (documents current behavior).
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest


class TestPhase2ExportConfig(unittest.TestCase):
    """T5 Review §1: config env int behavior."""

    def test_export_config_defaults_are_int_when_env_unset(self):
        """With clean env, PREDICTION_EXPORT_* are int and in reasonable range."""
        # Ensure env does not set these so we get defaults
        for key in ("PREDICTION_EXPORT_SAFETY_LAG_MINUTES", "PREDICTION_EXPORT_BATCH_ROWS"):
            os.environ.pop(key, None)
        # Re-import to pick up env (if already imported, we might get cached; test assumes fresh or same process)
        from trainer.core import config
        self.assertIsInstance(config.PREDICTION_EXPORT_SAFETY_LAG_MINUTES, int)
        self.assertIsInstance(config.PREDICTION_EXPORT_BATCH_ROWS, int)
        self.assertGreaterEqual(config.PREDICTION_EXPORT_SAFETY_LAG_MINUTES, 0)
        self.assertLess(config.PREDICTION_EXPORT_SAFETY_LAG_MINUTES, 60 * 24)
        self.assertGreater(config.PREDICTION_EXPORT_BATCH_ROWS, 0)

    def test_invalid_safety_lag_env_causes_failure_on_import(self):
        """Review §1: invalid PREDICTION_EXPORT_SAFETY_LAG_MINUTES causes process to fail (subprocess)."""
        code = """
import os
os.environ["PREDICTION_EXPORT_SAFETY_LAG_MINUTES"] = "not_a_number"
from trainer.core import config
print(config.PREDICTION_EXPORT_SAFETY_LAG_MINUTES)
"""
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertNotEqual(result.returncode, 0, "Invalid env should cause failure (current behavior)")


class TestPhase2RetentionConfig(unittest.TestCase):
    """T6 Review §1: PREDICTION_LOG_RETENTION_* config env int behavior."""

    def test_retention_config_defaults_are_int_when_env_unset(self):
        """With clean env, PREDICTION_LOG_RETENTION_* are int and in reasonable range."""
        for key in ("PREDICTION_LOG_RETENTION_DAYS", "PREDICTION_LOG_RETENTION_DELETE_BATCH"):
            os.environ.pop(key, None)
        from trainer.core import config
        self.assertIsInstance(config.PREDICTION_LOG_RETENTION_DAYS, int)
        self.assertIsInstance(config.PREDICTION_LOG_RETENTION_DELETE_BATCH, int)
        self.assertGreaterEqual(config.PREDICTION_LOG_RETENTION_DAYS, 0)
        self.assertGreater(config.PREDICTION_LOG_RETENTION_DELETE_BATCH, 0)

    def test_invalid_retention_days_env_causes_failure_on_import(self):
        """T6 Review §1: invalid PREDICTION_LOG_RETENTION_DAYS causes process to fail (subprocess)."""
        code = """
import os
os.environ["PREDICTION_LOG_RETENTION_DAYS"] = "not_a_number"
from trainer.core import config
print(config.PREDICTION_LOG_RETENTION_DAYS)
"""
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertNotEqual(result.returncode, 0, "Invalid env should cause failure (current behavior)")
