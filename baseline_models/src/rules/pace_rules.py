"""R1：活動下降（pace）規則分數（SSOT §4.1 R1）。"""

from __future__ import annotations

from typing import FrozenSet

import numpy as np
import pandas as pd

# IMPLEMENTATION_PLAN §4.2：與 SSOT §4.1 R1 欄位名一致
R1_PACE_SIGNAL_COLUMNS: FrozenSet[str] = frozenset(
    {
        "pace_drop_ratio",
        "pace_drop_ratio_w15m_w30m",
        "prev_bet_gap_min",
    }
)


def pace_rule_scores(frame: pd.DataFrame, signal: str) -> pd.Series:
    """由單一 pace 訊號欄產生規則排序分數（越高風險越高，與 SSOT 敘述一致）。

    Args:
        frame: 已通過契約之表（列與回傳序對齊）。
        signal: 欄名，須為 ``R1_PACE_SIGNAL_COLUMNS`` 之一。

    Returns:
        與 ``frame`` 索引對齊之浮點分數（越大越易判 walkaway）。

    Raises:
        ValueError: 訊號名不支援或欄位含 NaN。
        KeyError: 缺欄。
    """
    if signal not in R1_PACE_SIGNAL_COLUMNS:
        raise ValueError(
            f"不支援的 R1 pace 訊號: {signal!r}；允許: {sorted(R1_PACE_SIGNAL_COLUMNS)!r}"
        )
    if signal not in frame.columns:
        raise KeyError(
            f"pace R1 缺欄 {signal!r}；現有欄位: {list(frame.columns)!r}"
        )
    scores = pd.to_numeric(frame[signal], errors="coerce")
    if scores.isna().any():
        n_bad = int(scores.isna().sum())
        raise ValueError(f"pace 訊號 {signal!r} 含 NaN: count={n_bad}")
    return scores.astype(np.float64)


def empty_pace_score_placeholder(length: int) -> pd.Series:
    """Smoke 用佔位：全 NaN 分數（僅供介面測試，不得當正式 baseline）。

    Args:
        length: 列數。

    Returns:
        全為 ``numpy.nan`` 的 ``float`` 序列。
    """
    return pd.Series(np.full(length, np.nan, dtype=float), dtype=float)
