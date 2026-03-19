"""Guardrail tests for Code Review — Scorer Track Human lookback parity (STATUS.md).

Maps each Reviewer risk (§1–§2) to a minimal reproducible test.
Production code is not modified; tests document desired behavior.

§1: config source contract — scorer must use config from trainer (not cwd).
§2: boundary — when SCORER_LOOKBACK_HOURS is 0 or negative, build_features_for_scoring
    currently raises ValueError (from features.compute_loss_streak); test locks current
    behavior. When production adds fallback, update test to expect success.
"""

from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd

from trainer.serving import scorer as scorer_impl

HK_TZ = ZoneInfo("Asia/Hong_Kong")


def _minimal_bets_fixture():
    """Minimal bets DataFrame for build_features_for_scoring to reach Track Human block."""
    return pd.DataFrame({
        "bet_id": [1, 2],
        "session_id": ["s1", "s1"],
        "player_id": [100, 100],
        "table_id": ["t1", "t1"],
        "payout_complete_dtm": pd.to_datetime(["2026-03-01 11:00:00", "2026-03-01 11:05:00"]),
        "wager": [10.0, 20.0],
        "status": ["LOSE", "WIN"],
        "payout_odds": [1.9, 2.0],
        "base_ha": [0.02, 0.02],
        "is_back_bet": [1, 1],
        "position_idx": [0, 1],
    })


# ---------------------------------------------------------------------------
# §1 Config source contract (avoid cwd config shadowing)
# ---------------------------------------------------------------------------

class TestScorerLookbackConfigSourceContract(unittest.TestCase):
    """§1: Scorer module must use config from trainer (same style as DEC-030 validator)."""

    def test_scorer_config_source_is_trainer(self):
        """Contract: the config used by trainer.serving.scorer must have __name__ containing 'trainer'."""
        config = getattr(scorer_impl, "config", None)
        self.assertIsNotNone(config, "scorer module must have 'config' attribute")
        self.assertIn(
            "trainer",
            getattr(config, "__name__", ""),
            "scorer must use config from trainer (not cwd config.py)",
        )

    def test_scorer_config_has_scorer_lookback_hours(self):
        """Contract: config used by scorer must expose SCORER_LOOKBACK_HOURS (trainer SSOT)."""
        config = getattr(scorer_impl, "config", None)
        self.assertIsNotNone(config)
        val = getattr(config, "SCORER_LOOKBACK_HOURS", None)
        self.assertIsNotNone(val, "config.SCORER_LOOKBACK_HOURS must exist")
        self.assertIsInstance(val, (int, float), "SCORER_LOOKBACK_HOURS must be numeric")
        self.assertGreater(val, 0, "SCORER_LOOKBACK_HOURS must be positive")


# ---------------------------------------------------------------------------
# §2 lookback_hours <= 0 or non-numeric boundary (current: features raise)
# ---------------------------------------------------------------------------

class TestScorerLookbackHoursBoundary(unittest.TestCase):
    """§2: When SCORER_LOOKBACK_HOURS is 0 or negative, build_features_for_scoring behavior.

    Current production has no fallback; features.compute_loss_streak raises ValueError.
    If production adds fallback (e.g. warn + use 8), update tests to expect no raise.
    """

    def test_lookback_hours_zero_raises_value_error(self):
        """When SCORER_LOOKBACK_HOURS=0, build_features_for_scoring raises ValueError (from features)."""
        bets = _minimal_bets_fixture()
        sessions = pd.DataFrame()
        canonical_map = pd.DataFrame({"player_id": [100], "canonical_id": ["c100"]})
        cutoff = datetime(2026, 3, 1, 12, 0, 0, tzinfo=HK_TZ)

        with patch.object(scorer_impl.config, "SCORER_LOOKBACK_HOURS", 0):
            with self.assertRaises(ValueError) as ctx:
                scorer_impl.build_features_for_scoring(bets, sessions, canonical_map, cutoff)
            self.assertIn("lookback", str(ctx.exception).lower(), "error should mention lookback")

    def test_lookback_hours_negative_raises_value_error(self):
        """When SCORER_LOOKBACK_HOURS=-1, build_features_for_scoring raises ValueError (from features)."""
        bets = _minimal_bets_fixture()
        sessions = pd.DataFrame()
        canonical_map = pd.DataFrame({"player_id": [100], "canonical_id": ["c100"]})
        cutoff = datetime(2026, 3, 1, 12, 0, 0, tzinfo=HK_TZ)

        with patch.object(scorer_impl.config, "SCORER_LOOKBACK_HOURS", -1):
            with self.assertRaises(ValueError) as ctx:
                scorer_impl.build_features_for_scoring(bets, sessions, canonical_map, cutoff)
            self.assertIn("lookback", str(ctx.exception).lower(), "error should mention lookback")

    def test_lookback_hours_string_raises_or_completes(self):
        """When SCORER_LOOKBACK_HOURS is string '8', current code may raise TypeError (no coercion).

        Documents current behavior; if production adds type coercion, change to expect success.
        """
        bets = _minimal_bets_fixture()
        sessions = pd.DataFrame()
        canonical_map = pd.DataFrame({"player_id": [100], "canonical_id": ["c100"]})
        cutoff = datetime(2026, 3, 1, 12, 0, 0, tzinfo=HK_TZ)

        with patch.object(scorer_impl.config, "SCORER_LOOKBACK_HOURS", "8"):
            try:
                out = scorer_impl.build_features_for_scoring(bets, sessions, canonical_map, cutoff)
                self.assertIn("loss_streak", out.columns, "if no raise, output must have Track Human cols")
            except (TypeError, ValueError):
                # Current: features may raise when comparing "8" <= 0
                pass
