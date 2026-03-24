"""Simulation: four-bet timeline (9:55, 10:22, 10:28, 10:30) compares labels vs validator.

Task 6 alignment: remove validator early-FP branch `gap_started_before_alert` so 10:22
does not short-circuit to FP and can flow through standard gap + late-arrival paths.
"""

from __future__ import annotations

import types
import unittest
from datetime import datetime
from unittest.mock import patch

import pandas as pd
from zoneinfo import ZoneInfo

from trainer.labels import compute_labels
from trainer.serving import validator as validator_impl

import trainer.config as config

HK_TZ = ZoneInfo(config.HK_TZ)

# Wall-clock times (single day, HK)
D = "2020-06-15"


def _hk(hms: str) -> datetime:
    """Timezone-aware HK instant (validator bet_list / row)."""
    return pd.Timestamp(f"{D} {hms}").tz_localize(HK_TZ).to_pydatetime()


def _naive(hms: str) -> pd.Timestamp:
    """Naive HK local (compute_labels input convention)."""
    return pd.Timestamp(f"{D} {hms}")


class TestFourBetLabelVsValidatorSimulation(unittest.TestCase):
    """Same bet stream; trainer vs validator per-alert verdict at each bet_ts."""

    def test_1022_label_positive_validator_no_gap_started_before_alert_fp(self) -> None:
        """Task 6: 10:22 must not early-FP via gap_started_before_alert branch."""
        bets_df = pd.DataFrame(
            {
                "canonical_id": [1, 1, 1, 1],
                "bet_id": ["b955", "b1022", "b1028", "b1030"],
                "payout_complete_dtm": [
                    _naive("09:55:00"),
                    _naive("10:22:00"),
                    _naive("10:28:00"),
                    _naive("10:30:00"),
                ],
            }
        )

        window_end = _naive("11:00:00")
        extended_end = _naive("12:30:00")
        labeled = compute_labels(bets_df, window_end=window_end, extended_end=extended_end)

        # Map payout -> label for non-censored rows used in training
        by_time = labeled.set_index(
            pd.to_datetime(labeled["payout_complete_dtm"]).dt.strftime("%H:%M")
        )
        self.assertEqual(int(by_time.loc["09:55"]["label"]), 0)
        self.assertEqual(int(by_time.loc["10:22"]["label"]), 1)
        self.assertEqual(int(by_time.loc["10:28"]["label"]), 1)
        self.assertEqual(int(by_time.loc["10:30"]["label"]), 1)

        # Terminal row may be censored if extended_end tight; we chose extended_end so H1 holds
        self.assertFalse(bool(by_time.loc["10:30"]["censored"]), "terminal should be determinable")

        cid = "1"
        bet_list = [
            _hk("09:55:00"),
            _hk("10:22:00"),
            _hk("10:28:00"),
            _hk("10:30:00"),
        ]
        bet_cache = {cid: bet_list}
        session_cache: dict = {}

        # Fixed "now" so alert is not "too recent" and extended_end has passed for other paths
        fixed_now = _hk("18:00:00")

        row_1022 = pd.Series(
            {
                "ts": _hk("10:22:00"),
                "bet_ts": _hk("10:22:00"),
                "player_id": 1,
                "bet_id": "b1022",
                "canonical_id": cid,
                "score": 0.9,
            }
        )

        fake_dt = types.SimpleNamespace(now=lambda tz=None: fixed_now)
        with patch("trainer.serving.validator.datetime", fake_dt):
            res = validator_impl.validate_alert_row(
                row_1022, bet_cache, session_cache, force_finalize=True
            )

        self.assertNotEqual(
            res.get("reason"),
            "gap_started_before_alert",
            "Task 6 removes early-return reason for 10:22 path",
        )

    def test_other_bets_validator_not_early_gap_branch(self) -> None:
        """9:55 / 10:28 / 10:30 do not hit gap_started_before_alert (not 9:55->10:22 gap)."""
        cid = "1"
        bet_list = [
            _hk("09:55:00"),
            _hk("10:22:00"),
            _hk("10:28:00"),
            _hk("10:30:00"),
        ]
        bet_cache = {cid: bet_list}
        session_cache: dict = {}
        fixed_now = _hk("18:00:00")

        cases = [
            ("09:55", "b955"),
            ("10:28", "b1028"),
            ("10:30", "b1030"),
        ]
        for hms, bid in cases:
            with self.subTest(bet_ts=hms):
                row = pd.Series(
                    {
                        "ts": _hk(f"{hms}:00"),
                        "bet_ts": _hk(f"{hms}:00"),
                        "player_id": 1,
                        "bet_id": bid,
                        "canonical_id": cid,
                        "score": 0.9,
                    }
                )
                fake_dt = types.SimpleNamespace(now=lambda tz=None: fixed_now)
                with patch("trainer.serving.validator.datetime", fake_dt):
                    res = validator_impl.validate_alert_row(
                        row, bet_cache, session_cache, force_finalize=True
                    )
                self.assertNotEqual(
                    res.get("reason"),
                    "gap_started_before_alert",
                    f"{hms} should not early-FP for gap_started_before_alert",
                )


if __name__ == "__main__":
    unittest.main()
