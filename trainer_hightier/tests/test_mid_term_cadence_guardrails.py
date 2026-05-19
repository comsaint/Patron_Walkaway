"""Guardrail tests preventing mid-term cadence regressions in training suppliers."""

from __future__ import annotations

from pathlib import Path

import pytest

from trainer_hightier.feature_experiment.materialize_fe_derived import (
    LEGACY_MID_TERM_FEATURE_COLUMNS,
    materialize_fe_derived_short_term_parquet,
)


def test_legacy_mid_term_columns_documented_in_fe_derived() -> None:
    """Active model mid-term columns must be listed as legacy rolling outputs."""

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


def test_short_term_materializer_excludes_mid_term_model_columns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default trainer short-term parquet must not emit mid-term model columns."""

    import pandas as pd

    import trainer_hightier.feature_experiment.materialize_fe_derived as fe_mod

    full = tmp_path / "full.parquet"
    pd.DataFrame(
        {
            "bet_id": [1.0],
            "fe__wager_sum__w15m": [1.0],
            "fe__bets_cnt__w1d": [9.0],
            "fe__wager_cv_w7d": [0.5],
            "fe__payout_odds_z_prior_w30d": [0.1],
        }
    ).to_parquet(full, index=False)

    def _fake_full(**kwargs: object) -> Path:
        out = kwargs.get("out_parquet")
        assert out is not None
        pd.read_parquet(full).to_parquet(out, index=False)
        return Path(out)

    monkeypatch.setattr(fe_mod, "materialize_fe_derived_parquet", _fake_full)

    from trainer_hightier.config import DuckDbRuntimeConfig

    out = tmp_path / "short.parquet"
    fe_mod.materialize_fe_derived_short_term_parquet(
        cleaned_bet_parquet=tmp_path / "unused",
        training_parquet_for_bet_ids=tmp_path / "unused_train.parquet",
        out_parquet=out,
        duckdb_runtime=DuckDbRuntimeConfig(),
        short_term_columns=("fe__wager_sum__w15m",),
    )
    got_cols = set(pd.read_parquet(out).columns)
    assert got_cols == {"bet_id", "fe__wager_sum__w15m"}
