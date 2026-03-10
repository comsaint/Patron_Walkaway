"""Minimal reproducible tests for Scorer-defaults-in-config Code Review risks.

Review: STATUS.md "Scorer 預設移至 config — Code Review（目前變更）".
Run from repo root:
  python -m pytest tests/test_review_risks_scorer_defaults_in_config.py -v
"""

from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import trainer.config as config


def _make_parser_like_scorer():
    """Build parser with same defaults as scorer.main() (getattr(config, ...))."""
    p = argparse.ArgumentParser()
    p.add_argument(
        "--interval",
        type=int,
        default=getattr(config, "SCORER_POLL_INTERVAL_SECONDS", 45),
    )
    p.add_argument(
        "--lookback-hours",
        type=int,
        default=getattr(config, "SCORER_LOOKBACK_HOURS", 8),
    )
    p.add_argument("--once", action="store_true")
    p.add_argument("--model-dir", type=Path, default=None)
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING"])
    p.add_argument("--rebuild-canonical-mapping", action="store_true")
    return p


class TestScorerDefaultsFromConfig(unittest.TestCase):
    """Review #1/#3: CLI defaults must come from config; config must expose positive ints."""

    def test_cli_defaults_from_config(self):
        """When config is patched, parser defaults follow (contract: default=getattr(config, ...))."""
        with patch.object(config, "SCORER_LOOKBACK_HOURS", 4), patch.object(
            config, "SCORER_POLL_INTERVAL_SECONDS", 60
        ):
            parser = _make_parser_like_scorer()
            args = parser.parse_args(["--once"])
        self.assertEqual(args.lookback_hours, 4)
        self.assertEqual(args.interval, 60)


class TestScorerCliNonPositiveReproduceRisk(unittest.TestCase):
    """Review #2: Reproduce risk — CLI currently accepts 0 or negative (no validation)."""

    def test_cli_accepts_zero_lookback_hours_current_behavior(self):
        """Reproduce risk: --lookback-hours 0 is accepted; leads to empty window / wrong semantics."""
        parser = _make_parser_like_scorer()
        args = parser.parse_args(["--once", "--lookback-hours", "0"])
        self.assertEqual(args.lookback_hours, 0)

    def test_cli_accepts_negative_interval_current_behavior(self):
        """Reproduce risk: --interval -1 is accepted; leads to busy loop."""
        parser = _make_parser_like_scorer()
        args = parser.parse_args(["--once", "--interval", "-1"])
        self.assertEqual(args.interval, -1)


class TestScorerCliShouldRejectNonPositive(unittest.TestCase):
    """Review #2: Production rejects non-positive lookback-hours and interval via parser.error()."""

    def test_cli_rejects_non_positive_lookback_hours(self):
        """main() exits when --lookback-hours <= 0."""
        from trainer.scorer import main as scorer_main

        with patch.object(sys, "argv", ["scorer", "--once", "--lookback-hours", "0"]):
            with self.assertRaises(SystemExit):
                scorer_main()

    def test_cli_rejects_non_positive_interval(self):
        """main() exits when --interval <= 0."""
        from trainer.scorer import main as scorer_main

        with patch.object(sys, "argv", ["scorer", "--once", "--interval", "0"]):
            with self.assertRaises(SystemExit):
                scorer_main()


if __name__ == "__main__":
    unittest.main()
