"""S1：單特徵排名（無訓練），與 Tier-0 規則分列（SSOT §4.2）。"""

from __future__ import annotations

import numpy as np
import pandas as pd


def single_feature_scores(frame: pd.DataFrame, column: str, high_is_risk: bool) -> pd.Series:
    """對單一欄位產生「越高風險越高」之排序用分數。

    Args:
        frame: 資料表。
        column: 排序依據欄位。
        high_is_risk: 若為 False，則分數取負以統一成高風險高分。

    Returns:
        與 ``frame`` 索引對齊之 ``float64`` 分數（越大越易判 walkaway）。

    Raises:
        KeyError: 欄位不存在。
        ValueError: 欄位含 NaN 或無法轉為數值。
    """
    if column not in frame.columns:
        raise KeyError(f"single_feature_scores: 缺欄 {column!r}")
    s = pd.to_numeric(frame[column], errors="coerce")
    if s.isna().any():
        n_bad = int(s.isna().sum())
        raise ValueError(f"single_feature_scores: 欄 {column!r} 含 NaN 或無法轉數值；count={n_bad}")
    out = s if high_is_risk else -s
    return out.astype(np.float64)
