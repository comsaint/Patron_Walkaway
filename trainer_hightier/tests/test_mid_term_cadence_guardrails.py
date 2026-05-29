"""Guardrail tests preventing mid-term cadence regressions in training suppliers."""

from __future__ import annotations

from pathlib import Path
import pandas as pd
import pytest

from trainer_hightier.feature_experiment.materialize_fe_derived import (
    BOUNDED_SHORT_TERM_MATERIALIZER_VERSION,
    LEGACY_MID_TERM_FEATURE_COLUMNS,
    materialize_fe_derived_short_term_parquet,
)


def test_legacy_mid_term_columns_documented_in_fe_derived() -> None:
    """Former baseline mid-term columns must stay listed as legacy rolling outputs."""

    active_mid = {
        "fe__bets_cnt__w1d",
        "fe__wager_sum__w15m_over_w1d",
        "fe__wager_cv_w7d",
        "fe__payout_odds_z_prior_w30d",
        "fe__interarrival__last_gap_z__w7d",
    }
    missing = sorted(active_mid - set(LEGACY_MID_TERM_FEATURE_COLUMNS))
    assert not missing, f"legacy mid-term list missing {missing}"


def test_materialize_fe_derived_source_uses_range_preceding_for_mid_windows() -> None:
    """Legacy materializer still contains per-bet RANGE windows (quarantined path only)."""

    src = Path(__file__).resolve().parents[1] / "feature_experiment" / "materialize_fe_derived.py"
    text = src.read_text(encoding="utf-8")
    assert "RANGE BETWEEN INTERVAL '1 DAY' PRECEDING" in text
    assert "LEGACY_MID_TERM_FEATURE_COLUMNS" in text


def test_short_term_materializer_uses_bounded_hot_pool_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default trainer short-term parquet must use bounded hot pool materialization."""

    import trainer_hightier.feature_experiment.materialize_fe_derived as fe_mod

    train = tmp_path / "train.parquet"
    pd.DataFrame(
        {
            "bet_id": [1.0],
            "player_id": [10],
            "payout_complete_dtm": [pd.Timestamp("2024-06-01 10:00:00", tz="UTC")],
            "wager": [1.0],
            "is_back_bet": [0],
            "payout_odds": [2.0],
            "casino_win": [0.0],
            "bet_type": ["PLAYER"],
            "type_of_bet": ["MAIN"],
            "session_id": [1],
            "table_id": [1],
            "gaming_day_event": [pd.Timestamp("2024-06-01")],
        },
    ).to_parquet(train, index=False)

    out = tmp_path / "short.parquet"
    captured: dict[str, object] = {}

    def _fake_iter(*args: object, **kwargs: object):
        captured["batch_size"] = kwargs.get("batch_size")
        yield pd.read_parquet(train)

    def _fake_batch(*args: object, **kwargs: object) -> pd.DataFrame:
        row: dict[str, object] = {"bet_id": 1.0}
        for col in kwargs.get("trial_columns", ()):
            row[str(col)] = 0.0
        for col in kwargs.get("fe_columns", ()):
            row[str(col)] = 1.0
        return pd.DataFrame([row])

    monkeypatch.setattr(fe_mod, "_iter_training_bet_batches", _fake_iter)
    monkeypatch.setattr(fe_mod, "_short_term_features_for_batch", _fake_batch)

    from trainer_hightier.config import DuckDbRuntimeConfig

    fe_mod.materialize_fe_derived_short_term_parquet(
        cleaned_bet_parquet=tmp_path / "unused",
        training_parquet_for_bet_ids=train,
        out_parquet=out,
        duckdb_runtime=DuckDbRuntimeConfig(),
        short_term_columns=("fe__wager_sum__w15m",),
        trial_columns=(),
    )
    assert captured.get("batch_size") == 2000
    src = Path(__file__).resolve().parents[1] / "feature_experiment" / "materialize_fe_derived.py"
    assert BOUNDED_SHORT_TERM_MATERIALIZER_VERSION in src.read_text(encoding="utf-8")
    got_cols = set(pd.read_parquet(out).columns)
    assert got_cols == {"bet_id", "fe__wager_sum__w15m"}
