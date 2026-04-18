"""特徵視圖：為各 baseline 從主表選欄、組 pace／loss／ADT 相關欄位集。"""

from __future__ import annotations

from typing import Sequence

import numpy as np
import pandas as pd


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
    return frame.loc[:, list(columns)].copy()


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
    sub = select_feature_subset(frame, columns)
    num = sub.apply(pd.to_numeric, errors="coerce")
    if bool(num.isna().any().any()):
        bad = int(num.isna().sum().sum())
        raise ValueError(
            f"numeric_feature_matrix: 特徵含 NaN 或無法轉數值；bad_cell_count={bad}; columns={list(columns)!r}"
        )
    return num.to_numpy(dtype=np.float64, copy=False)
