"""Tests for flight recorder DataFrame diff engine."""

from __future__ import annotations

import pandas as pd

from trainer_hightier.serving.flight_recorder.diff import diff_dataframes


def test_diff_detects_added_removed_changed() -> None:
    """Diff reports key-level changes between two frames."""
    left = pd.DataFrame(
        {
            "bet_id": ["1", "2"],
            "__ts_ms": [100, 200],
            "wager": [10.0, 20.0],
        }
    )
    right = pd.DataFrame(
        {
            "bet_id": ["2", "3"],
            "__ts_ms": [250, 300],
            "wager": [20.0, 30.0],
        }
    )
    report = diff_dataframes(left, right, business_key="bet_id")
    assert report["added_keys_count"] == 1
    assert report["removed_keys_count"] == 1
    assert report["changed_keys_count"] == 1
    assert report["added_keys_sample"] == ["3"]
    assert report["removed_keys_sample"] == ["1"]
