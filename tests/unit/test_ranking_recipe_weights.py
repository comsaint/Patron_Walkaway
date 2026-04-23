"""Unit tests for precision uplift A2 ranking recipe sample weights."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from trainer.training.ranking_recipe_weights import (
    RANKING_RECIPE_BASELINE,
    RANKING_RECIPE_COMBINED,
    RANKING_RECIPE_HNM,
    RANKING_RECIPE_TOP_BAND,
    apply_ranking_recipe_pre_optuna_weights,
    apply_top_band_reweighting,
    refine_weights_hnm_shallow_lgbm,
    resolve_ranking_recipe,
)


def test_resolve_ranking_recipe_unknown_falls_back_to_baseline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRECISION_UPLIFT_RANKING_RECIPE", raising=False)
    assert resolve_ranking_recipe("not-a-recipe") == RANKING_RECIPE_BASELINE


def test_resolve_ranking_recipe_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRECISION_UPLIFT_RANKING_RECIPE", "r2_top_band_light")
    assert resolve_ranking_recipe(None) == RANKING_RECIPE_TOP_BAND


def test_resolve_ranking_recipe_defaults_to_top_band_when_cli_and_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PRECISION_UPLIFT_RANKING_RECIPE", raising=False)
    assert resolve_ranking_recipe(None) == RANKING_RECIPE_TOP_BAND


def test_resolve_ranking_recipe_explicit_baseline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRECISION_UPLIFT_RANKING_RECIPE", "r2_combined_light")
    assert resolve_ranking_recipe("baseline") == RANKING_RECIPE_BASELINE


def test_baseline_is_identity() -> None:
    df = pd.DataFrame(
        {
            "label": [0, 1, 0, 1],
            "f1": [1.0, 2.0, 3.0, 4.0],
            "f2": [0.0, 1.0, 0.5, 2.0],
        }
    )
    base = pd.Series([1.0, 2.0, 1.0, 2.0], index=df.index)
    sw, meta = apply_ranking_recipe_pre_optuna_weights(df, base, RANKING_RECIPE_BASELINE, ["f1", "f2"])
    pd.testing.assert_series_equal(sw, base.astype(float), check_names=False)
    assert meta["ranking_recipe"] == RANKING_RECIPE_BASELINE


def test_top_band_increases_some_negative_weights() -> None:
    rng = np.random.default_rng(0)
    n = 200
    df = pd.DataFrame(
        {
            "label": rng.integers(0, 2, size=n),
            "x": rng.normal(size=n),
        }
    )
    base = pd.Series(1.0, index=df.index)
    sw, meta = apply_top_band_reweighting(df, base, ["x"])
    assert float(sw.max()) >= float(base.max())
    assert meta["ranking_recipe_top_band_neg_boosted"] >= 1


def test_combined_applies_both_stages_in_pre_optuna() -> None:
    df = pd.DataFrame(
        {
            "label": [0] * 80 + [1] * 20,
            "a": list(range(80)) + list(range(20, 40)),
        }
    )
    base = pd.Series(1.0, index=df.index)
    sw, meta = apply_ranking_recipe_pre_optuna_weights(df, base, RANKING_RECIPE_COMBINED, ["a"])
    assert meta["ranking_recipe"] == RANKING_RECIPE_COMBINED
    assert meta.get("ranking_recipe_top_band_neg_boosted", 0) >= 1
    assert meta.get("ranking_recipe_pseudo_hnm_neg_boosted", 0) >= 1
    assert float(sw.max()) > 1.0


def test_refine_hnm_skips_single_class() -> None:
    df = pd.DataFrame({"x": [1.0, 2.0], "y": [0.0, 0.0]})
    sw = pd.Series([1.0, 1.0], index=df.index)
    params = {
        "objective": "binary",
        "verbose": -1,
        "random_state": 42,
        "n_estimators": 50,
        "learning_rate": 0.1,
        "max_depth": 4,
        "num_leaves": 15,
        "min_child_samples": 5,
    }
    sw2, meta = refine_weights_hnm_shallow_lgbm(df[["x"]], df["y"], sw, params)
    pd.testing.assert_series_equal(sw2, sw.astype(float), check_names=False)
    assert meta.get("ranking_recipe_hnm_skipped") == "single_class_or_empty"


def test_refine_hnm_boosts_some_negatives() -> None:
    rng = np.random.default_rng(1)
    n = 120
    x = rng.normal(size=(n, 3))
    y = rng.integers(0, 2, size=n)
    df = pd.DataFrame({"f0": x[:, 0], "f1": x[:, 1], "f2": x[:, 2], "label": y})
    sw = pd.Series(1.0, index=df.index)
    params = {
        "objective": "binary",
        "verbose": -1,
        "random_state": 42,
        "n_estimators": 80,
        "learning_rate": 0.05,
        "max_depth": 5,
        "num_leaves": 31,
        "min_child_samples": 10,
        "colsample_bytree": 0.8,
        "subsample": 0.8,
        "subsample_freq": 1,
        "reg_alpha": 0.01,
        "reg_lambda": 0.01,
    }
    sw2, meta = refine_weights_hnm_shallow_lgbm(
        df[["f0", "f1", "f2"]],
        df["label"],
        sw,
        params,
        n_estimators_cap=80,
    )
    assert int(meta.get("ranking_recipe_hnm_shallow_neg_boosted", 0)) >= 1
    assert float(sw2.max()) > 1.0
