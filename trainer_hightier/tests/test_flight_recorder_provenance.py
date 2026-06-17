"""Tests for feature missing provenance serialization."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from trainer_hightier.serving.feature_supply import ScorerSupplierPlan
from trainer_hightier.serving.flight_recorder.provenance import build_feature_missing_provenance
from trainer_hightier.serving.flight_recorder.parquet_io import write_parquet_safe


def _minimal_plan() -> ScorerSupplierPlan:
    return ScorerSupplierPlan(
        baseline_cols=("wager", "bet_type"),
        feast_trial_cols=(),
        feast_mid_cols=(),
        feast_slow_cols=(),
        mid_composite_cols=(),
        short_term_cols=(),
        unknown_cols=(),
    )


def test_provenance_writes_categorical_values_to_parquet(tmp_path: Path) -> None:
    """Mixed numeric and categorical feature values serialize to string Parquet."""
    staged = pd.DataFrame({"bet_id": ["1"], "canonical_id": ["c1"]})
    features = pd.DataFrame({"wager": [100.0], "bet_type": ["SMALL_DRAGON"]})
    prov = build_feature_missing_provenance(
        staged,
        features,
        feature_columns=("wager", "bet_type"),
        supplier_plan=_minimal_plan(),
    )
    out = tmp_path / "feature_missing_provenance.parquet"
    write_parquet_safe(out, prov)
    loaded = pd.read_parquet(out)
    assert loaded.loc[loaded["feature_id"] == "bet_type", "feature_value"].iloc[0] == "SMALL_DRAGON"
    assert loaded.loc[loaded["feature_id"] == "wager", "feature_value"].iloc[0] == "100.0"
