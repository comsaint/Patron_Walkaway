"""tests/test_review_risks_round140.py
======================================
Minimal reproducible guardrail tests for Round 34 review risks (R200-R207).

Tests-only: no production code changes.
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import unittest
from datetime import datetime, timedelta
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pandas as pd
from zoneinfo import ZoneInfo

from trainer.trainer import ensure_player_profile_ready, run_pipeline

HK_TZ = ZoneInfo("Asia/Hong_Kong")


class TestR200SchemaHashHorizonGuardrail(unittest.TestCase):
    """R200: schema hash should differ when max_lookback_days differs."""

    def test_max_lookback_days_change_should_invalidate_existing_profile_cache(self):
        with TemporaryDirectory() as td:
            from pathlib import Path

            local_dir = Path(td)
            profile_path = local_dir / "player_profile.parquet"
            session_path = local_dir / "gmwds_t_session.parquet"
            sidecar_path = local_dir / "player_profile.schema_hash"

            profile_path.write_text("dummy", encoding="utf-8")
            session_path.write_text("dummy", encoding="utf-8")

            # Existing cache written under old/default horizon semantics.
            base_hash = "deadbeefdeadbeefdeadbeefdeadbeef"
            # ensure_player_profile_ready encodes population + horizon + schedule tags
            stored_hash = hashlib.md5((base_hash + "_full" + "_mlb=365" + "_month_end").encode()).hexdigest()
            sidecar_path.write_text(stored_hash, encoding="utf-8")

            with (
                patch("trainer.trainer.LOCAL_PARQUET_DIR", local_dir),
                patch("trainer.trainer.LOCAL_PROFILE_SCHEMA_HASH", sidecar_path),
                patch("trainer.trainer.compute_profile_schema_hash", return_value=base_hash),
                patch(
                    "trainer.trainer._parquet_date_range",
                    side_effect=[
                        # session range
                        (datetime(2025, 1, 1).date(), datetime(2025, 1, 31).date()),
                        # existing profile range
                        (datetime(2025, 1, 1).date(), datetime(2025, 1, 31).date()),
                    ],
                ),
            ):
                ensure_player_profile_ready(
                    window_start=datetime(2025, 1, 10, tzinfo=HK_TZ),
                    window_end=datetime(2025, 1, 20, tzinfo=HK_TZ),
                    use_local_parquet=True,
                    max_lookback_days=30,
                )

            # Desired behavior: horizon changed (old cache hash without horizon tag),
            # so cache must be invalidated (deleted) before continuing.
            self.assertFalse(
                profile_path.exists(),
                "Profile cache should be invalidated when max_lookback_days changes",
            )


class TestR207FeatureTrackClassificationGuardrail(unittest.TestCase):
    """R207: save_artifact_bundle should classify profile subset as track='profile'."""

    def test_feature_list_track_classification_for_profile_subset(self):
        from pathlib import Path
        import json
        import trainer.trainer as trainer_mod

        feature_cols = ["loss_streak", "days_since_last_session", "sessions_30d"]

        with TemporaryDirectory() as td:
            model_dir = Path(td)
            with patch.object(trainer_mod, "MODEL_DIR", model_dir):
                trainer_mod.save_artifact_bundle(
                    rated=None,
                    feature_cols=feature_cols,
                    combined_metrics={},
                    model_version="test-v1",
                )

            feature_list = json.loads((model_dir / "feature_list.json").read_text(encoding="utf-8"))
            track_by_name = {item["name"]: item["track"] for item in feature_list}
            self.assertEqual(track_by_name["loss_streak"], "B")
            self.assertEqual(track_by_name["days_since_last_session"], "profile")
            self.assertEqual(track_by_name["sessions_30d"], "profile")

if __name__ == "__main__":
    unittest.main()

