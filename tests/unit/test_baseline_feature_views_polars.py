from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from baseline_models.src import feature_views as fv


def _sample_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "f1": [1, 2, 3],
            "f2": [0.5, 1.5, 2.5],
            "f3": ["4", "5", "6"],
            "label": [0, 1, 0],
        }
    )


def test_select_feature_subset_missing_column_raises_keyerror() -> None:
    df = _sample_frame()
    with pytest.raises(KeyError, match="feature_views: 缺欄"):
        fv.select_feature_subset(df, ["f1", "missing"])


def test_numeric_feature_matrix_contract_dtype_shape_and_order() -> None:
    df = _sample_frame()
    mat = fv.numeric_feature_matrix(df, ["f3", "f1"])
    assert mat.shape == (3, 2)
    assert mat.dtype == np.float64
    np.testing.assert_allclose(mat[:, 0], np.array([4.0, 5.0, 6.0], dtype=np.float64))
    np.testing.assert_allclose(mat[:, 1], np.array([1.0, 2.0, 3.0], dtype=np.float64))


def test_numeric_feature_matrix_invalid_value_raises_valueerror() -> None:
    df = _sample_frame().copy()
    df.loc[1, "f3"] = "bad"
    with pytest.raises(ValueError, match="bad_cell_count"):
        fv.numeric_feature_matrix(df, ["f3", "f1"])


def test_polars_path_matches_pandas_reference_for_subset_and_matrix(monkeypatch: pytest.MonkeyPatch) -> None:
    pl = pytest.importorskip("polars")
    assert pl is not None
    df = _sample_frame()
    monkeypatch.setattr(fv, "_FEATURE_VIEWS_ENGINE", "pandas")
    subset_pd = fv.select_feature_subset(df, ["f2", "f1"])
    matrix_pd = fv.numeric_feature_matrix(df, ["f3", "f1"])
    monkeypatch.setattr(fv, "_FEATURE_VIEWS_ENGINE", "polars")
    subset_pl = fv.select_feature_subset(df, ["f2", "f1"])
    matrix_pl = fv.numeric_feature_matrix(df, ["f3", "f1"])
    pd.testing.assert_frame_equal(subset_pd.reset_index(drop=True), subset_pl.reset_index(drop=True))
    np.testing.assert_allclose(matrix_pd, matrix_pl, rtol=0.0, atol=0.0)
