"""Phase C: prediction_skip admission contract + train-serve-backtest parity."""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

_REPO = Path(__file__).resolve().parents[2]
_CANDIDATES = _REPO / "trainer" / "feature_spec" / "feature_spec.yaml"
_DEPLOY = _REPO / "package" / "deploy" / "models" / "feature_spec.yaml"


@pytest.fixture(autouse=True)
def _drop_model_dir_env():
    os.environ.pop("MODEL_DIR", None)
    yield


def _make_bets(pit_rated, canonical_ids):
    return pd.DataFrame(
        {
            "bet_id": list(range(1, len(pit_rated) + 1)),
            "player_id": [f"p{i}" for i in range(len(pit_rated))],
            "canonical_id": list(canonical_ids),
            "payout_complete_dtm": pd.to_datetime(["2024-01-01 12:00"] * len(pit_rated)),
            "_pit_rated": list(pit_rated),
        }
    )


def test_admission_constants_match_specs() -> None:
    """Gate-C2: helper reason codes are a superset of both specs' admission_rule."""
    from trainer.features import layered as L

    for p in (_CANDIDATES, _DEPLOY):
        spec = yaml.safe_load(p.read_text(encoding="utf-8"))
        rule = L.get_admission_rule_from_spec(spec)
        assert rule.get("on_pit_unavailable") == "prediction_skip", p
        codes = set(rule.get("skip_reason_codes") or [])
        assert codes.issubset(L.VALID_SKIP_REASON_CODES), (p, codes)
        assert L.validate_admission_rule_against_spec(spec) == [], p


def test_admission_pit_only_paths() -> None:
    """PIT path: _pit_rated False -> PIT_UNAVAILABLE_SOURCE."""
    from trainer.features import layered as L

    bets = _make_bets(
        pit_rated=[True, False, True, True],
        canonical_ids=["a", "b", "c", "d"],
    )
    res = L.evaluate_pit_admission(bets)
    assert res.total_input_rows == 4
    assert res.admitted_rows == 3
    assert res.skip_counts == {L.SKIP_REASON_PIT_UNAVAILABLE_SOURCE: 1}
    assert "_pit_rated" not in res.admitted.columns
    assert list(res.admitted["bet_id"]) == [1, 3, 4]


def test_admission_cutoff_window_paths() -> None:
    """cutoff_window path (no _pit_rated): unmatched canonical -> IDENTITY_UNMATCHED."""
    from trainer.features import layered as L

    bets = pd.DataFrame(
        {
            "bet_id": [1, 2, 3],
            "canonical_id": ["a", "b", "c"],
        }
    )
    res = L.evaluate_pit_admission(bets, rated_canonical_ids={"a", "c"})
    assert res.skip_counts == {L.SKIP_REASON_IDENTITY_UNMATCHED: 1}
    assert list(res.admitted["bet_id"]) == [1, 3]


def test_admission_missing_required_input_takes_priority() -> None:
    """Order: MISSING_REQUIRED_INPUT > PIT_UNAVAILABLE_SOURCE > IDENTITY_UNMATCHED."""
    from trainer.features import layered as L

    bets = pd.DataFrame(
        {
            "bet_id": [1, 2, 3, 4],
            "canonical_id": ["a", "b", "c", "d"],
            "_pit_rated": [True, False, True, True],
            "payout_complete_dtm": [pd.Timestamp("2024-01-01"), pd.NaT, pd.Timestamp("2024-01-01"), pd.Timestamp("2024-01-01")],
        }
    )
    res = L.evaluate_pit_admission(
        bets,
        required_input_cols=["payout_complete_dtm"],
        rated_canonical_ids={"a"},
    )
    assert res.skip_counts == {
        L.SKIP_REASON_MISSING_REQUIRED_INPUT: 1,
        L.SKIP_REASON_IDENTITY_UNMATCHED: 2,
    }
    assert list(res.admitted["bet_id"]) == [1]


def test_admission_empty_input() -> None:
    from trainer.features import layered as L

    bets = pd.DataFrame({"bet_id": [], "canonical_id": []})
    res = L.evaluate_pit_admission(bets, rated_canonical_ids={"a"})
    assert res.total_input_rows == 0
    assert res.admitted_rows == 0
    assert res.skip_counts == {}


def test_admission_to_log_dict_keys() -> None:
    from trainer.features import layered as L

    bets = _make_bets(pit_rated=[True, False], canonical_ids=["a", "b"])
    res = L.evaluate_pit_admission(bets)
    d = res.to_log_dict()
    assert set(d) == {"total_input_rows", "admitted_rows", "skipped_rows", "skip_counts"}
    assert d["skipped_rows"] + d["admitted_rows"] == d["total_input_rows"]


@pytest.mark.parametrize("mod_name", [
    "trainer.training.trainer",
    "trainer.serving.scorer",
    "trainer.training.backtester",
])
def test_pit_admission_entrypoints_use_layered_helper(mod_name: str) -> None:
    """Gate-C1: trainer/scorer/backtester all import the unified admission helper."""
    mod = __import__(mod_name, fromlist=["evaluate_pit_admission"])
    fn = getattr(mod, "evaluate_pit_admission", None)
    assert fn is not None, f"{mod_name} must expose evaluate_pit_admission"
    assert fn.__module__ == "trainer.features.layered"


def test_skip_reason_code_is_distinct_from_shap_reason_codes() -> None:
    """Gate-C4: ``skip_reason_code`` (admission) and ``reason_codes`` (SHAP) must
    NOT collide as columns / contracts."""
    from trainer.features import layered as L

    bets = _make_bets(pit_rated=[True, False], canonical_ids=["a", "b"])
    res = L.evaluate_pit_admission(bets)
    assert "skip_reason_code" not in res.admitted.columns
    assert "reason_codes" not in res.admitted.columns
    for code in res.skip_counts:
        assert code in L.VALID_SKIP_REASON_CODES
        assert code != "reason_codes"


def test_admission_train_serve_backtest_parity() -> None:
    """Gate-C3: same input + same rated set -> same admitted bet_ids across all
    three callers (they all delegate to the same helper)."""
    import trainer.training.trainer as TR
    import trainer.serving.scorer as SC
    import trainer.training.backtester as BT

    bets = _make_bets(
        pit_rated=[True, False, True, True],
        canonical_ids=["a", "b", "c", "z"],
    )
    rated = {"a", "b", "c"}

    def _ids(fn, df):
        res = fn(df.copy(), rated_canonical_ids=rated)
        return list(res.admitted["bet_id"])

    ids_tr = _ids(TR.evaluate_pit_admission, bets)
    ids_sc = _ids(SC.evaluate_pit_admission, bets)
    ids_bt = _ids(BT.evaluate_pit_admission if hasattr(BT, "evaluate_pit_admission") else SC.evaluate_pit_admission, bets)
    assert ids_tr == ids_sc == ids_bt
    assert ids_tr == [1, 3]
