"""特徵視圖：為各 baseline 從主表選欄、組 pace／loss／ADT 相關欄位集。"""

from __future__ import annotations

import os
from typing import Sequence

import numpy as np
import pandas as pd

try:
    import polars as pl
except ModuleNotFoundError:  # pragma: no cover - exercised by environment without polars
    pl = None


_FEATURE_VIEWS_ENGINE = (os.getenv("BASELINE_FEATURE_VIEWS_ENGINE", "auto") or "auto").strip().lower()


def _use_polars_path() -> bool:
    if _FEATURE_VIEWS_ENGINE == "pandas":
        return False
    if _FEATURE_VIEWS_ENGINE == "polars":
        return pl is not None
    return pl is not None


def _select_feature_subset_pandas(frame: pd.DataFrame, columns: Sequence[str]) -> pd.DataFrame:
    return frame.loc[:, list(columns)].copy()


def _select_feature_subset_polars(frame: pd.DataFrame, columns: Sequence[str]) -> pd.DataFrame:
    assert pl is not None
    pldf = pl.from_pandas(frame, include_index=False)
    # Keep the public contract unchanged: downstream still receives pandas.
    return pldf.select([pl.col(c) for c in columns]).to_pandas().copy()


def _numeric_feature_matrix_pandas(frame: pd.DataFrame, columns: Sequence[str]) -> np.ndarray:
    sub = _select_feature_subset_pandas(frame, columns)
    num = sub.apply(pd.to_numeric, errors="coerce")
    if bool(num.isna().any().any()):
        bad = int(num.isna().sum().sum())
        raise ValueError(
            f"numeric_feature_matrix: 特徵含 NaN 或無法轉數值；bad_cell_count={bad}; columns={list(columns)!r}"
        )
    return num.to_numpy(dtype=np.float64, copy=False)


def _numeric_feature_matrix_polars(frame: pd.DataFrame, columns: Sequence[str]) -> np.ndarray:
    assert pl is not None
    pldf = pl.from_pandas(frame, include_index=False).select([pl.col(c) for c in columns])
    casted = pldf.with_columns([pl.col(c).cast(pl.Float64, strict=False).alias(c) for c in columns])
    bad = 0
    for c in columns:
        s = casted.get_column(c)
        bad += int(s.null_count())
        if s.dtype in (pl.Float32, pl.Float64):
            bad += int(s.is_nan().sum())
    if bad > 0:
        raise ValueError(
            f"numeric_feature_matrix: 特徵含 NaN 或無法轉數值；bad_cell_count={bad}; columns={list(columns)!r}"
        )
    return casted.to_numpy().astype(np.float64, copy=False)


def select_feature_subset(frame: pd.DataFrame, columns: Sequence[str]) -> pd.DataFrame:
    """回傳僅含指定欄之子集（缺欄時 fail-fast）。

    Args:
        frame: 來源表。
        columns: 欲保留之欄位名序列。

    Returns:
        子集副本。

    Raises:
        KeyError: 任一欄不存在。
    """
    missing = [c for c in columns if c not in frame.columns]
    if missing:
        raise KeyError(f"feature_views: 缺欄 missing={missing!r}")
    if _use_polars_path():
        return _select_feature_subset_polars(frame, columns)
    return _select_feature_subset_pandas(frame, columns)


def numeric_feature_matrix(frame: pd.DataFrame, columns: Sequence[str]) -> np.ndarray:
    """選欄並轉為 ``float64`` 稠密矩陣（供 sklearn）；含 NaN 則 fail-fast。

    Args:
        frame: 來源表。
        columns: 特徵欄位名（順序即矩陣欄序）。

    Returns:
        形狀 ``(n_rows, n_features)`` 之 ``float64`` 陣列。

    Raises:
        KeyError: 缺欄（委託 :func:`select_feature_subset`）。
        ValueError: 任一儲存格無法轉數值或為 NaN。
    """
    missing = [c for c in columns if c not in frame.columns]
    if missing:
        raise KeyError(f"feature_views: 缺欄 missing={missing!r}")
    if _use_polars_path():
        return _numeric_feature_matrix_polars(frame, columns)
    return _numeric_feature_matrix_pandas(frame, columns)
