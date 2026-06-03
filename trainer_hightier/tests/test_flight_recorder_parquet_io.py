"""Tests for schema-aware Parquet serialization in flight recorder."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from trainer_hightier.serving.feature_supply import ScorerSupplierPlan
from trainer_hightier.serving.flight_recorder.parquet_io import (
    prepare_dataframe_for_parquet,
    write_parquet_safe,
)
from trainer_hightier.serving.flight_recorder.provenance import build_feature_missing_provenance


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


def test_t_bet_like_frame_round_trip(tmp_path: Path) -> None:
    """Raw t_bet columns with String enums and Decimal amounts write without dtype errors."""
    frame = pd.DataFrame(
        {
            "bet_id": [101001],
            "bet_type": ["SMALL_DRAGON"],
            "type_of_bet": ["SIDE_BET"],
            "status": ["WIN"],
            "wager": [500.0],
            "player_id": [172596],
            "game_id": [315892327],
            "session_id": [101001],
            "table_id": [1005],
            "__deleted": [False],
            "__op": ["c"],
        }
    )
    out = tmp_path / "t_bet.final.parquet"
    write_parquet_safe(out, frame)
    loaded = pd.read_parquet(out)
    assert loaded["bet_type"].iloc[0] == "SMALL_DRAGON"
    assert loaded["__deleted"].iloc[0] == "False"
    assert float(loaded["wager"].iloc[0]) == 500.0


def test_mixed_object_column_coerced_to_string(tmp_path: Path) -> None:
    """Object columns mixing str and numeric serialize as string, not double."""
    frame = pd.DataFrame({"misc_col": ["SMALL_DRAGON", "PLAYER", None]})
    safe = prepare_dataframe_for_parquet(frame)
    assert safe["misc_col"].dtype == "string"
    out = tmp_path / "misc.parquet"
    write_parquet_safe(out, frame)
    loaded = pd.read_parquet(out)
    assert loaded["misc_col"].iloc[0] == "SMALL_DRAGON"


def test_numeric_object_column_stays_float(tmp_path: Path) -> None:
    """Purely numeric object columns coerce to float64."""
    frame = pd.DataFrame({"amount": ["1.5", "2.0", None]})
    safe = prepare_dataframe_for_parquet(frame)
    assert safe["amount"].dtype == "float64"


def test_stage_features_with_category_dtype(tmp_path: Path) -> None:
    """Stage snapshot with categorical bet_type writes successfully."""
    frame = pd.DataFrame(
        {
            "bet_id": [1, 2],
            "bet_type": pd.Categorical(["BANKER", "SMALL_DRAGON"]),
            "wager": [100.0, 200.0],
            "score": [0.01, 0.99],
        }
    )
    out = tmp_path / "stage_09_scores.parquet"
    write_parquet_safe(out, frame)
    loaded = pd.read_parquet(out)
    assert set(loaded["bet_type"].astype(str)) == {"BANKER", "SMALL_DRAGON"}


def test_provenance_mixed_feature_values(tmp_path: Path) -> None:
    """Provenance with numeric + categorical feature_value round-trips via safe writer."""
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


def test_decision_trace_bool_result(tmp_path: Path) -> None:
    """Validator decision trace with bool result column writes cleanly."""
    trace = pd.DataFrame(
        {
            "bet_id": ["90001"],
            "canonical_id": ["cp1"],
            "result": [False],
            "reason": ["no_bet_found"],
            "gap_minutes": [12.5],
        }
    )
    out = tmp_path / "decision_trace.parquet"
    write_parquet_safe(out, trace)
    loaded = pd.read_parquet(out)
    assert not bool(loaded["result"].iloc[0])


def test_direct_to_parquet_would_fail_on_mixed_feature_value() -> None:
    """Document why raw to_parquet fails on mixed-type feature_value column."""
    import io

    prov = pd.DataFrame(
        {
            "feature_id": ["wager", "bet_type"],
            "feature_value": [100.0, "SMALL_DRAGON"],
        }
    )
    with pytest.raises(Exception):
        prov.to_parquet(io.BytesIO(), index=False)
