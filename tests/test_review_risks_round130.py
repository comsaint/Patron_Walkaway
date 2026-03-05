"""tests/test_review_risks_round130.py
======================================
Minimal reproducible guardrail tests for Round 31 review risks (R120-R123).

Tests-only: no production code changes.
"""

from __future__ import annotations

import unittest
from datetime import date, datetime
from tempfile import TemporaryDirectory
from unittest.mock import patch, MagicMock

import pandas as pd
from pandas.testing import assert_frame_equal
from zoneinfo import ZoneInfo

from trainer import etl_player_profile
from trainer.features import (
    PROFILE_FEATURE_COLS,
    _PROFILE_FEATURE_MIN_DAYS,
    get_profile_feature_cols,
)
from trainer.trainer import ensure_player_profile_ready

HK_TZ = ZoneInfo("Asia/Hong_Kong")


class TestR120CanonicalMapInprocessGuardrail(unittest.TestCase):
    """R120: local-parquet normal mode should still forward canonical_map in-process."""

    def test_normal_mode_local_parquet_with_canonical_map_uses_inprocess_backfill(self):
        with TemporaryDirectory() as td:
            from pathlib import Path

            local_dir = Path(td)
            # Gate check in ensure_player_profile_ready: session parquet must exist.
            (local_dir / "gmwds_t_session.parquet").touch()

            cmap = pd.DataFrame(
                {
                    "player_id": [1, 2],
                    "canonical_id": ["A", "B"],
                }
            )

            with patch("trainer.trainer.LOCAL_PARQUET_DIR", local_dir), patch(
                "trainer.trainer._parquet_date_range",
                side_effect=[
                    (date(2025, 1, 1), date(2025, 1, 31)),  # session range
                    None,  # existing profile range -> missing
                    None,  # final profile range after build attempt
                ],
            ), patch("trainer.trainer._etl_backfill") as mock_backfill, patch(
                "trainer.trainer.subprocess.run",
                return_value=MagicMock(returncode=0, stderr="", stdout=""),
            ):
                ensure_player_profile_ready(
                    window_start=datetime(2025, 1, 10, tzinfo=HK_TZ),
                    window_end=datetime(2025, 1, 20, tzinfo=HK_TZ),
                    use_local_parquet=True,
                    canonical_id_whitelist=None,  # normal mode
                    snapshot_interval_days=1,  # normal mode
                    preload_sessions=True,
                    canonical_map=cmap,
                )

            # Guardrail expectation (currently FAILS before production fix):
            # canonical_map is provided, so backfill should run in-process and receive it.
            mock_backfill.assert_called_once()
            self.assertIs(mock_backfill.call_args.kwargs.get("canonical_map"), cmap)


class TestR121WhitelistMutationGuardrail(unittest.TestCase):
    """R121: backfill whitelist filtering must not mutate caller canonical_map."""

    @patch("trainer.etl_player_profile.build_player_profile", return_value=None)
    def test_backfill_whitelist_does_not_mutate_caller_canonical_map(self, _mock_build):
        original = pd.DataFrame(
            {
                "player_id": [1, 2, 3, 4, 5],
                "canonical_id": ["A", "B", "C", "D", "E"],
            }
        )
        before = original.copy(deep=True)

        etl_player_profile.backfill(
            start_date=date(2025, 1, 1),
            end_date=date(2025, 1, 1),
            use_local_parquet=False,
            canonical_id_whitelist={"A", "C"},
            snapshot_interval_days=1,
            preload_sessions=False,
            canonical_map=original,
        )

        assert_frame_equal(original, before)


class TestR122FeatureMapCoverageGuardrail(unittest.TestCase):
    """R122: _PROFILE_FEATURE_MIN_DAYS must cover PROFILE_FEATURE_COLS exactly."""

    def test_profile_feature_min_days_covers_all_cols(self):
        self.assertSetEqual(set(_PROFILE_FEATURE_MIN_DAYS.keys()), set(PROFILE_FEATURE_COLS))


class TestR123ProfileFeatureEdgeCasesGuardrail(unittest.TestCase):
    """R123: document/lock edge behavior for get_profile_feature_cols()."""

    def test_get_profile_feature_cols_edge_cases(self):
        f0 = get_profile_feature_cols(0)
        f1 = get_profile_feature_cols(1)
        f7 = get_profile_feature_cols(7)
        f365 = get_profile_feature_cols(365)

        self.assertEqual(f0, [])
        self.assertIn("days_since_last_session", f1)
        self.assertIn("days_since_first_session", f1)
        self.assertNotIn("sessions_7d", f1)
        self.assertIn("sessions_7d", f7)
        self.assertEqual(f365, PROFILE_FEATURE_COLS)


if __name__ == "__main__":
    unittest.main()
