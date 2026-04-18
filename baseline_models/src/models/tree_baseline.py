"""Tier-2 可選：淺層樹（O1）；GaussianNB 若實作可置於獨立模組或延伸此檔。"""

from __future__ import annotations

from typing import Any

import pandas as pd


def fit_shallow_tree_baseline(
    _train_x: pd.DataFrame,
    _train_y: pd.Series,
    max_depth: int = 4,
    **_kwargs: Any,
) -> Any:
    """訓練淺層決策樹對照（骨架）。

    Args:
        _train_x: 訓練特徵。
        _train_y: 訓練標籤。
        max_depth: 樹深度上限（SSOT §4.3 建議小網格）。
        **_kwargs: 其餘估計器參數。

    Returns:
        已擬合之估計器。

    Raises:
        NotImplementedError: 可選項尚未接線。
    """
    raise NotImplementedError(f"fit_shallow_tree_baseline：O1 可選實作（max_depth={max_depth}）。")
