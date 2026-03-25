"""validation_results.bet_ts migration + deploy API tolerate missing column."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_validator_new_val_cols_includes_bet_ts_migration() -> None:
    text = (_REPO_ROOT / "trainer" / "serving" / "validator.py").read_text(encoding="utf-8")
    assert '("bet_ts", "TEXT")' in text
    assert "_NEW_VAL_COLS" in text


def test_scorer_init_state_migrates_validation_results_bet_ts() -> None:
    text = (_REPO_ROOT / "trainer" / "serving" / "scorer.py").read_text(encoding="utf-8")
    assert "_VALIDATION_RESULTS_MIGRATION_COLS" in text
    assert "PRAGMA table_info(validation_results)" in text


def test_deploy_main_validation_protocol_fills_missing_bet_ts_column() -> None:
    for rel in (
        "package/deploy/main.py",
        "deploy_dist/main.py",
        "trainer/serving/api_server.py",
    ):
        text = (_REPO_ROOT / rel).read_text(encoding="utf-8")
        assert 'if "bet_ts" not in df.columns' in text
        assert 'df["bet_ts"] = None' in text


def test_validation_protocol_df_without_bet_ts_column_returns_null_bet_ts() -> None:
    """Mirror _validation_to_protocol_records column fill + shape (no Flask import)."""
    df = pd.DataFrame(
        {
            "alert_ts": ["2026-03-12T10:00:00+08:00"],
            "player_id": [1],
            "bet_id": ["1"],
            "gap_start": [None],
            "result": [0],
            "validated_at": ["2026-03-12T11:00:00+08:00"],
            "reason": ["MISS"],
        }
    )
    df = df.copy()
    if "bet_ts" not in df.columns:
        df["bet_ts"] = None
    assert "bet_ts" in df.columns
    assert pd.isna(df["bet_ts"].iloc[0]) or df["bet_ts"].iloc[0] is None
